"""Durable cross-run engagement-ledger tests (Bug 2 fix).

Verify the incremental achieved-hops ledger (maintained in code, zero LLM inference) survives across
separate MythicTools instances (== separate solves/runs) via a per-engagement JSON file:

- engagement_state.hops_to_dicts / hops_from_dicts round-trip a Hop list losslessly.
- A hop recorded + persisted by one MythicTools is auto-loaded by a fresh instance (cross-run resume).
- With the gate OFF, __init__ never reads disk (byte-for-byte no-op on the load side).

No live Mythic is required — MythicTools.__init__ does no network (login is separate). Persistence is
pure filesystem I/O against a tmp dir. Mirrors the repo's no-pytest-asyncio convention.
"""

from tests.esl_fixtures import seed_ledger  # noqa: E402
import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_ledger as el  # noqa: E402
import engagement_state  # noqa: E402
import mythic_tools  # noqa: E402


def _arm_runtime_lineage(mt, task_id="450", callback_id="50", command="test-command"):
    mt._last_issued_task_display_id = task_id
    mt._last_issued_callback_id = callback_id
    mt._last_issued_task_terminal_status = "completed"
    mt._last_issued_command = command


def _record_success(mt, output):
    mt._record_engagement_success(
        output,
        task_id=mt._last_issued_task_display_id,
        callback_id=mt._last_issued_callback_id,
        terminal_status=mt._last_issued_task_terminal_status,
        command=mt._last_issued_command,
    )


def _gpo_hop():
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "gpo-abuse",
        "winterfell.north.sevenkingdoms.local",
        "achieved",
        {"source": "test", "scheduled_task_present": True},
        "2026-06-07T00:00:00Z",
    )
    return state.hops


# ---------------------------------------------------------------------------
# 1. Serialize / deserialize round-trip
# ---------------------------------------------------------------------------


def test_hops_dict_roundtrip_is_lossless():
    hops = _gpo_hop()
    dicts = engagement_state.hops_to_dicts(hops)
    assert isinstance(dicts, list) and dicts and isinstance(dicts[0], dict)
    # Must be JSON-serializable (this is what the durable ledger writes).
    back = engagement_state.hops_from_dicts(json.loads(json.dumps(dicts)))
    assert len(back) == len(hops)
    a, b = hops[0], back[0]
    for fld in ("id", "technique", "target", "effect", "status",
                "preconditions", "satisfied_effects", "source", "timestamp"):
        assert getattr(a, fld) == getattr(b, fld)
    assert a.evidence == b.evidence


def test_graph_facts_dict_roundtrip_is_lossless():
    facts = [
        engagement_state.GraphFact(
            "generic-write:gpo:starkwallpaper",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
    ]

    dicts = engagement_state.graph_facts_to_dicts(facts)
    back = engagement_state.graph_facts_from_dicts(json.loads(json.dumps(dicts)))

    assert back == facts


def test_from_dicts_skips_malformed_entries():
    out = engagement_state.hops_from_dicts(["nope", 5, None, {"technique": "x"}])
    assert len(out) == 1  # only the dict yields a Hop (with safe defaults)
    assert out[0].technique == "x"


def test_to_dicts_skips_non_dataclass():
    assert engagement_state.hops_to_dicts(["nope", 5, None]) == []


# ---------------------------------------------------------------------------
# 2. Cross-run resume: instance A records -> instance B auto-loads
# ---------------------------------------------------------------------------


def _store():
    import engagement_store
    return engagement_store.EngagementStore()


def _settings(namespace):
    store = _store()
    return store.legacy_settings(store.bind_namespace(namespace))


def _set_objective(namespace, objective, source=""):
    store = _store()
    store.update_objective(store.bind_namespace(namespace), objective, source)


def test_cross_run_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-eng")

    # First session records an achieved hop -> one row in the shared store.
    mt1 = mythic_tools.MythicTools(agent_task_id="solve-1")
    _arm_runtime_lineage(mt1)
    assert mt1._engagement_hops == []
    mt1._pending_engagement_hop = (
        "gpo-abuse", "winterfell.north.sevenkingdoms.local", "2026-06-07T00:00:00Z",
    )
    _record_success(mt1, "whoami\r\nnt authority\\system\r\n")
    rows = mt1._ledger_snapshot().display_hops()
    assert mt1._ledger_snapshot().engagement_key == "test-eng"
    assert [row["technique"] for row in rows] == ["gpo-abuse"] and rows[0]["number"] == 1

    # A second session (new solve) reads the same row; it was not produced by this session -> durable.
    mt2 = mythic_tools.MythicTools(agent_task_id="solve-2")
    hop = next(h for h in mt2._engagement_hops if h.technique == "gpo-abuse")
    assert hop.status == "achieved" and hop.evidence["provenance"] == "durable"


def test_cross_run_resume_restores_graph_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-graph")

    mt1 = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt1._ledger_commit_facts([
        engagement_state.GraphFact(
            "generic-write:gpo:starkwallpaper",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-07T00:00:00Z",
            600,
        ),
    ], complete=True, refreshed_at="2026-06-07T00:00:00Z")

    mt2 = mythic_tools.MythicTools(agent_task_id="solve-2")
    assert [fact.predicate for fact in mt2._engagement_graph_facts] == [
        "generic-write:gpo:starkwallpaper",
        "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
    ]
    assert mt2._engagement_graph_facts_ts == "2026-06-07T00:00:00Z"

    state = engagement_state.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[
            engagement_state.Foothold(
                callback_id="3",
                agent="apollo",
                host="CASTELBLACK",
                forest="north.sevenkingdoms.local",
                identity="NORTH\\samwell.tarly",
                integrity="medium",
                alive=True,
                source="test",
                timestamp="2026-06-07T00:00:00Z",
            )
        ],
        graph_facts=list(mt2._engagement_graph_facts),
    )
    rendered = engagement_state.render_engagement_state(state)
    assert "=== ENGAGEMENT STATE" in rendered
    assert "CASTELBLACK" in rendered
    assert "Phase:" not in rendered
    assert "NEXT GROUNDED ACTIONS" not in rendered


def test_hop_writes_never_touch_the_objective_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "objective-test")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    _set_objective("objective-test", "obtain administrative control of essos.local")

    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt._engagement_objective() == "obtain administrative control of essos.local"
    _arm_runtime_lineage(mt)
    mt._pending_engagement_hop = ("gpo-abuse", "winterfell.north.sevenkingdoms.local", "2026-06-07T00:00:00Z")
    _record_success(mt, "whoami\r\nnt authority\\system\r\n")
    assert _settings("objective-test")["objective"] == "obtain administrative control of essos.local"
    assert "objective" not in mt._ledger_store().export(mt._require_ledger_binding())


def test_running_objective_refreshes_when_state_command_updates_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "objective-refresh")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")

    _set_objective("objective-refresh", "obtain administrative control of north.sevenkingdoms.local")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt._engagement_objective() == "obtain administrative control of north.sevenkingdoms.local"

    _set_objective("objective-refresh", "obtain administrative control of essos.local", "operator")
    assert mt._engagement_objective() == "obtain administrative control of essos.local"


def test_opaque_ledger_objective_is_not_treated_as_human_goal(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "objective-opaque")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    _set_objective("objective-opaque", "sage-engagement:older-task")

    mt = mythic_tools.MythicTools(agent_task_id="solve-2")
    assert mt._engagement_objective() == "sage-engagement:solve-2"


def test_fresh_instance_reads_the_shared_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "test-eng2")

    seed = mythic_tools.MythicTools(agent_task_id="seed")
    _arm_runtime_lineage(seed)
    seed._pending_engagement_hop = (
        "gpo-abuse", "winterfell.north.sevenkingdoms.local", "2026-06-07T00:00:00Z",
    )
    _record_success(seed, "whoami\r\nnt authority\\system\r\n")

    fresh = mythic_tools.MythicTools(agent_task_id="fresh")
    assert any(getattr(h, "technique", "") == "gpo-abuse" for h in fresh._engagement_hops)


# ---------------------------------------------------------------------------
# Autonomous-solve objective adoption through the temporary objective-settings bridge. Never overrides
# operator/env/legacy objectives; writes only the separate settings table, never ledger rows.
# ---------------------------------------------------------------------------

_SEED = "escalate to Domain Admin of north.sevenkingdoms.local and DCSync its krbtgt account"


def test_autonomous_seed_adopted_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-adopt")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = _SEED
    assert mt._engagement_objective() == _SEED
    assert _settings("seed-adopt") == {"objective": _SEED, "objective_source": "autonomous_seed"}
    fresh = mythic_tools.MythicTools(agent_task_id="solve-2")
    assert fresh._engagement_objective() == _SEED


def test_autonomous_seed_never_overrides_operator_objective(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-precedence")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    _set_objective("seed-precedence", "obtain administrative control of essos.local")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = _SEED
    assert mt._engagement_objective() == "obtain administrative control of essos.local"
    assert _settings("seed-precedence")["objective"] == "obtain administrative control of essos.local"


def test_env_objective_wins_over_autonomous_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-env")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "env mission: control example.local")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = _SEED
    assert mt._engagement_objective() == "env mission: control example.local"
    assert not _settings("seed-env").get("objective")


def test_autonomous_seed_not_persisted_before_the_ledger_binds(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default")   # no namespace: needs an operation
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    assert mt._engagement_key is None and mt._eng_key() == ""
    mt._autonomous_objective_seed = _SEED
    # In-memory for the running solve, but nothing is written anywhere until the session binds.
    assert mt._engagement_objective() == _SEED
    assert mt._autonomous_objective_persisted is False
    store = _store()
    mt._ledger_binding = store.bind_namespace("resolved-op")
    mt._engagement_key = mt._ledger_binding.engagement_key
    assert mt._engagement_objective() == _SEED
    assert mt._autonomous_objective_persisted is True
    assert _settings("resolved-op")["objective"] == _SEED


def test_no_seed_keeps_opaque_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-none")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    mt = mythic_tools.MythicTools(agent_task_id="solve-9")
    assert mt._engagement_objective() == "sage-engagement:solve-9"
    assert mt._autonomous_objective_persisted is False


def test_operator_objective_binding_reads_current_operator_setting_only(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "stored-trigger")
    mt = mythic_tools.MythicTools(agent_task_id="solve-stored-trigger")
    objective = "collect and ingest the graph, then read available credentials"

    _set_objective("stored-trigger", objective, "operator")
    assert mt.operator_objective_binding() == objective

    _set_objective("stored-trigger", "a replacement operator objective", "operator")
    assert mt.operator_objective_binding() == "a replacement operator objective"

    _set_objective("stored-trigger", "a replacement operator objective", "autonomous_seed")
    assert mt.operator_objective_binding() == ""

    _set_objective("stored-trigger", "a replacement operator objective", "")
    assert mt.operator_objective_binding() == ""

    def unavailable(_binding):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mt._ledger_store(), "legacy_settings", unavailable)
    assert mt.operator_objective_binding() == ""


def test_operator_objective_binding_requires_a_bound_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "stored-trigger")
    mt = mythic_tools.MythicTools(agent_task_id="solve-stored-trigger")
    mt._ledger_binding = None

    assert mt.operator_objective_binding() == ""


def test_reused_client_new_autonomous_seed_supersedes_prior_autonomous(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-reuse")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = "mission A: DA of north.sevenkingdoms.local"
    assert mt._engagement_objective() == "mission A: DA of north.sevenkingdoms.local"
    assert _settings("seed-reuse")["objective_source"] == "autonomous_seed"
    mt._autonomous_objective_seed = "mission B: control essos.local"
    mt._autonomous_objective_persisted = False
    assert mt._engagement_objective() == "mission B: control essos.local"
    assert _settings("seed-reuse") == {"objective": "mission B: control essos.local",
                                        "objective_source": "autonomous_seed"}


def test_operator_objective_sticky_against_autonomous_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-op-sticky")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    _set_objective("seed-op-sticky", "obtain administrative control of essos.local", "operator")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = "mission: DA of north.sevenkingdoms.local"
    assert mt._engagement_objective() == "obtain administrative control of essos.local"
    assert _settings("seed-op-sticky")["objective"] == "obtain administrative control of essos.local"


def test_legacy_objective_without_provenance_is_sticky(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-legacy")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    _set_objective("seed-legacy", "legacy objective on example.local")   # NO provenance (legacy)
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = "mission: something else"
    assert mt._engagement_objective() == "legacy objective on example.local"
    assert _settings("seed-legacy")["objective"] == "legacy objective on example.local"


def test_autonomous_seed_persist_retries_after_transient_store_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-retry")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = "mission: DA of north.sevenkingdoms.local"
    store = mt._ledger_store()
    real_update = store.update_objective
    calls = {"n": 0}

    def flaky_update(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient disk error")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(store, "update_objective", flaky_update)
    assert mt._engagement_objective() == "mission: DA of north.sevenkingdoms.local"
    assert mt._autonomous_objective_persisted is False
    assert mt._engagement_objective() == "mission: DA of north.sevenkingdoms.local"
    assert mt._autonomous_objective_persisted is True
    assert _settings("seed-retry")["objective"] == "mission: DA of north.sevenkingdoms.local"


def test_autonomous_seed_persists_exactly_once_then_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-once")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = "mission: DA of north.sevenkingdoms.local"
    store = mt._ledger_store()
    real_update = store.update_objective
    writes = {"n": 0}

    def counting_update(*args, **kwargs):
        writes["n"] += 1
        return real_update(*args, **kwargs)

    monkeypatch.setattr(store, "update_objective", counting_update)
    for _ in range(5):
        assert mt._engagement_objective() == "mission: DA of north.sevenkingdoms.local"
    assert writes["n"] == 1


def test_operator_objective_overrides_prior_autonomous_seed_mid_run(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "seed-op-override")
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "")
    mt = mythic_tools.MythicTools(agent_task_id="solve-1")
    mt._autonomous_objective_seed = "auto mission: DA of north.sevenkingdoms.local"
    assert mt._engagement_objective() == "auto mission: DA of north.sevenkingdoms.local"
    assert _settings("seed-op-override")["objective_source"] == "autonomous_seed"
    _set_objective("seed-op-override", "obtain administrative control of essos.local", "operator")
    assert mt._engagement_objective() == "obtain administrative control of essos.local"
    assert _settings("seed-op-override")["objective"] == "obtain administrative control of essos.local"


# --- knob A: coerce a free-handed native dcsync into the only form proven to dump a hash in-lab ---

def _dcsync_mt(monkeypatch, tmp_path, dc="WINTERFELL.north.sevenkingdoms.local"):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "Operation_DCSync")
    mt = mythic_tools.MythicTools(agent_task_id="solve-dcsync")
    async def _fake_resolve(domain):  # stand in for the live BloodHound DC lookup
        return dc
    monkeypatch.setattr(mt, "_resolve_domain_controller_host", _fake_resolve)
    return mt


def test_dcsync_freeform_string_coerced_to_working_dict(tmp_path, monkeypatch):
    import asyncio
    mt = _dcsync_mt(monkeypatch, tmp_path)
    out = asyncio.run(mt._coerce_native_dcsync_to_working_form("dcsync", "north.sevenkingdoms.local krbtgt"))
    assert out == {"domain": "north.sevenkingdoms.local", "user": "NORTH\\krbtgt",
                   "dc": "WINTERFELL.north.sevenkingdoms.local"}


def test_dcsync_dashflag_string_coerced(tmp_path, monkeypatch):
    import asyncio
    mt = _dcsync_mt(monkeypatch, tmp_path)
    out = asyncio.run(mt._coerce_native_dcsync_to_working_form("dcsync", "-Domain north.sevenkingdoms.local -User krbtgt"))
    assert out == {"domain": "north.sevenkingdoms.local", "user": "NORTH\\krbtgt",
                   "dc": "WINTERFELL.north.sevenkingdoms.local"}


def test_dcsync_dcless_dict_gets_dc_injected_and_user_qualified(tmp_path, monkeypatch):
    import asyncio
    mt = _dcsync_mt(monkeypatch, tmp_path)
    out = asyncio.run(mt._coerce_native_dcsync_to_working_form("dcsync", {"domain": "north.sevenkingdoms.local", "user": "krbtgt"}))
    assert out == {"domain": "north.sevenkingdoms.local", "user": "NORTH\\krbtgt",
                   "dc": "WINTERFELL.north.sevenkingdoms.local"}


def test_dcsync_already_good_form_preserved(tmp_path, monkeypatch):
    import asyncio
    mt = _dcsync_mt(monkeypatch, tmp_path, dc="meereen.essos.local")
    good = {"domain": "essos.local", "user": "ESSOS\\administrator", "dc": "meereen.essos.local"}
    out = asyncio.run(mt._coerce_native_dcsync_to_working_form("dcsync", dict(good)))
    assert out == good  # already-qualified user + present dc are left intact


def test_dcsync_fail_open_when_dc_unresolved(tmp_path, monkeypatch):
    import asyncio
    mt = _dcsync_mt(monkeypatch, tmp_path, dc="")  # BloodHound can't resolve a DC
    out = asyncio.run(mt._coerce_native_dcsync_to_working_form("dcsync", "north.sevenkingdoms.local krbtgt"))
    assert out == {"domain": "north.sevenkingdoms.local", "user": "NORTH\\krbtgt"}  # no dc key, no crash


def test_coercer_ignores_non_dcsync_command(tmp_path, monkeypatch):
    import asyncio
    mt = _dcsync_mt(monkeypatch, tmp_path)
    assert asyncio.run(mt._coerce_native_dcsync_to_working_form("shell", "whoami /all")) == "whoami /all"


# --- collection-ingest idempotency by content hash (no re-ingest of the identical zip) ---

def test_collection_ingest_idempotency_by_content_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "Op_Ingest")
    mt = mythic_tools.MythicTools(agent_task_id="solve-ingest")
    content = b"PK\x03\x04 fake-sharphound-collection-bytes"

    h, prior = mt._collection_already_ingested(content)
    assert prior is None                              # first sight: not cached
    mt._record_collection_ingested(h, "bh-job-77")    # a verified ingest
    h2, prior2 = mt._collection_already_ingested(content)
    assert h2 == h and prior2 == "bh-job-77"          # identical bytes -> known, skip re-upload
    _, prior3 = mt._collection_already_ingested(content + b"x")
    assert prior3 is None                             # different artifact -> not suppressed


def test_bloodhound_collected_domains_ignores_stub_domains():
    class FakeInfoTool:
        async def ainvoke(self, args):
            assert args["info_type"] == "list"
            return [{
                "type": "text",
                "text": json.dumps({
                    "data": [
                        {"name": "TARGET.LOCAL", "collected": True},
                        {"name": "SOURCE.LOCAL", "collected": False},
                    ]
                }),
            }]

    domains = asyncio.run(mythic_tools._bloodhound_collected_domains(FakeInfoTool()))

    assert domains == ["target.local"]


def test_file_uuid_ingest_infers_source_callback_for_graph_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "Op_Ingest_Callback")
    mt = mythic_tools.MythicTools(agent_task_id="solve-ingest-callback")
    mt.client = object()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("20260623_users.json", json.dumps({"data": []}) + (" " * 128))
    content = archive.getvalue()
    content_hash, _ = mt._collection_already_ingested(content)
    mt._record_collection_ingested(content_hash, "bh-job-88")
    recorded = []

    async def fake_metadata(file_uuid):
        assert file_uuid == "downloaded-file-uuid"
        return {"task": {"display_id": 772, "command_name": "download", "callback": {"display_id": 50}}}

    async def fake_download(*args, **kwargs):
        return content

    async def fake_record_graph(callback_display_id, verified, covered_domains=None, collection_scope_domain="", proof_envelope=None):
        assert proof_envelope
        recorded.append((callback_display_id, verified, covered_domains, collection_scope_domain))

    monkeypatch.setattr(mt, "_get_file_metadata", fake_metadata)
    monkeypatch.setattr(mythic_tools.mythic, "download_file", fake_download)
    monkeypatch.setattr(mt, "_record_graph_built", fake_record_graph)
    async def fake_covered_domains(info_tool=None):
        return ["north.example.local"]
    monkeypatch.setattr(mythic_tools, "_bloodhound_collected_domains", fake_covered_domains)

    result = json.loads(asyncio.run(mt.ingest_collection(
        file_uuid="downloaded-file-uuid",
        file_name="bloodhound.zip",
    )))

    assert result["status"] == "already_ingested"
    assert result["graph_verified"] is True
    assert result["source_callback_display_id"] == 50
    assert result["covered_domains"] == ["north.example.local"]
    assert recorded == [(50, True, ["north.example.local"], "")]


def test_dcsync_mimikatz_slashflag_string_coerced(tmp_path, monkeypatch):
    import asyncio
    mt = _dcsync_mt(monkeypatch, tmp_path)
    out = asyncio.run(mt._coerce_native_dcsync_to_working_form(
        "dcsync", "lsadump::dcsync /domain:north.sevenkingdoms.local /user:krbtgt"))
    assert out == {"domain": "north.sevenkingdoms.local", "user": "NORTH\\krbtgt",
                   "dc": "WINTERFELL.north.sevenkingdoms.local"}


def test_dcsync_domain_charset_sanitized_against_cypher_injection(tmp_path, monkeypatch):
    import asyncio
    seen = {}
    mt = _dcsync_mt(monkeypatch, tmp_path)
    async def _capture(domain):
        seen["domain"] = domain
        return "dc.north.sevenkingdoms.local"
    monkeypatch.setattr(mt, "_resolve_domain_controller_host", _capture)
    asyncio.run(mt._coerce_native_dcsync_to_working_form(
        "dcsync", {"domain": "north.sevenkingdoms.local' OR '1'='1", "user": "krbtgt"}))
    assert "'" not in seen["domain"] and " " not in seen["domain"]  # sanitized before the cypher lookup
