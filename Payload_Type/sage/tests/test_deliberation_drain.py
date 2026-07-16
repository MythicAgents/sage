"""Tests for the deliberation-drain guards in MythicTools.

Root cause covered: a 2026-06-09 clean solve made 330 internal tool calls to do ONE productive Mythic
task before hitting the 300-step limit — issue_task ×168 (gate-DEFERred re-proposals re-fired), and
get_all_commands_for_payloadtype ×27 re-dumping the full schema into context. These pin the two fixes:
  1. command schemas cache per payloadtype -> a repeat returns a terse pointer, not the full schema
  2. a hop the gate keeps blocking is STOPped after 3 tries (reset when a task actually passes the gate)
Run: cd Payload_Type/sage && python3 -m pytest tests/test_deliberation_drain.py -q
"""
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402
import engagement_state as es  # noqa: E402
import proof_boundary as pb  # noqa: E402
from mythic_tools import MythicTools  # noqa: E402

NOW = "2026-06-09T12:00:00Z"


def _make_tools() -> MythicTools:
    mt = MythicTools(agent_task_id="test-task")
    mt.client = object()  # truthy so client-None guards pass
    return mt


@contextmanager
def _split_issue(output):
    async def fake_issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        return {"display_id": 4242}

    async def fake_waitfor(mythic, task_display_id, timeout=None):
        return output

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue_task), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", fake_waitfor):
        yield


# --- command-schema cache ---------------------------------------------------------------------------

def test_command_schema_cache_returns_terse_on_repeat():
    mt = _make_tools()
    mt._command_schema_cache["apollo"] = [{"cmd": "execute_assembly"}, {"cmd": "ls"}, {"cmd": "whoami"}]
    out = asyncio.run(mt.get_all_commands_for_payloadtype("apollo"))
    data = json.loads(out)
    assert data["cached"] is True
    assert data["command_count"] == 3
    assert "execute_assembly" in data["commands"]
    # The terse pointer must NOT carry the bulky per-command parameter schema.
    assert "commandparameters" not in out


def test_command_schema_uncached_payload_not_terse():
    mt = _make_tools()
    mt._command_schema_cache["apollo"] = [{"cmd": "ls"}]
    # merlin is not cached -> must NOT return the terse cached shape (it will try to fetch -> error w/o a
    # real client, which is fine; the point is it is not served from apollo's cache).
    out = asyncio.run(mt.get_all_commands_for_payloadtype("merlin"))
    assert '"cached": true' not in out.lower()


# --- issue-hook skip handling -----------------------------------------------------------------------

# Stage B retired the STRIPS gate breaker. A hook-level SKIP still short-circuits tasking, but repeated
# identical skips are reported as the same hook decision instead of escalating to a breaker STOP.
def _block_gate(_nudge="[engagement-gate] skipped: effect already achieved (run): creds:krbtgt@essos.local"):
    async def gate(command, parameters, callback_display_id):
        return _nudge
    return gate


def test_issue_hook_skip_short_circuits_without_breaker_stop(monkeypatch):
    mt = _make_tools()
    mt._engagement_issue_hook = _block_gate()
    params = {"domain": "essos.local"}
    r1 = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", params, 4))
    r2 = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", params, 4))
    r3 = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", params, 4))
    assert "skipped" in r1 and "skipped" in r2 and "skipped" in r3
    assert not r3.startswith("STOP")


def test_issue_hook_skip_is_independent_per_action(monkeypatch):
    mt = _make_tools()
    mt._engagement_issue_hook = _block_gate()
    asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", {"domain": "essos.local"}, 4))
    asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", {"domain": "essos.local"}, 4))
    # A different blocked action must not be converted into a synthetic breaker STOP.
    r = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", {"domain": "north.sevenkingdoms.local"}, 4))
    assert not r.startswith("STOP")


def test_issue_hook_skip_allows_later_pass_and_later_skip(monkeypatch):
    mt = _make_tools()
    state = {"block": True}

    async def gate(command, parameters, callback_display_id):
        return "[engagement-gate] skipped: x" if state["block"] else None

    mt._engagement_issue_hook = gate
    asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", {"domain": "d"}, 4))  # block 1
    asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", {"domain": "d"}, 4))  # block 2
    state["block"] = False
    with _split_issue("ok"):  # a task passes the hook -> progress proceeds normally
        asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 4))
    state["block"] = True
    r = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", {"domain": "d"}, 4))
    assert "skipped" in r and not r.startswith("STOP")


# --- credential-store cache + corroboration probe ---------------------------------------------------

def test_fetch_credentials_cached_serves_from_cache():
    mt = _make_tools()
    mt._cred_cache = [{"account": "samwell", "realm": "north.sevenkingdoms.local", "credential_text": "h"}]
    mt._cred_cache_ts = NOW
    # Fresh cache + same timestamp -> returns the cache without querying Mythic (client untouched).
    out = asyncio.run(mt._fetch_credentials_cached(NOW))
    assert out == mt._cred_cache


def test_corroboration_facts_from_cred_store_and_graph():
    mt = _make_tools()
    mt._cred_cache = [
        {"account": "krbtgt", "realm": "north.sevenkingdoms.local", "credential_text": "deadbeef"},
        {"account": "samwell.tarly", "realm": "north.sevenkingdoms.local", "credential_text": "x"},
        {"account": "SEVENKINGDOMS\\cersei.lannister", "realm": "sevenkingdoms.local", "credential_text": "x"},
        {"account": "empty", "realm": "north.sevenkingdoms.local", "credential_text": ""},  # no secret -> skip
    ]
    mt._cred_cache_ts = NOW
    mt._engagement_graph_facts = [
        es.GraphFact("generic-write:gpo:starkwallpaper", "bloodhound:cypher", NOW, 600),
        es.GraphFact("gpo-domain:starkwallpaper:north.sevenkingdoms.local", "bloodhound:cypher", NOW, 600),
    ]
    facts = {f.predicate for f in asyncio.run(mt._corroboration_facts(NOW))}
    assert "krbtgt-hash:north.sevenkingdoms.local" in facts          # dcsync artifact
    assert "creds:samwell.tarly@north.sevenkingdoms.local" in facts  # dumped-cred artifact
    assert "creds:cersei.lannister@sevenkingdoms.local" in facts     # NETBIOS-qualified accounts canonicalize
    assert "creds:sevenkingdoms\\cersei.lannister@sevenkingdoms.local" not in facts
    assert "system:starkwallpaper" in facts                          # gpo-abuse still-controlled artifact
    assert "gpo-domain:starkwallpaper:north.sevenkingdoms.local" in facts  # chains gpo-abuse->dcsync IN THE GATE
    assert not any(p.startswith("generic-write:") for p in facts)    # never emit precondition prefixes
    assert "creds:empty@north.sevenkingdoms.local" not in facts      # no secret -> not corroborated


def test_runtime_corroboration_admits_only_task_linked_credential_rows():
    mt = _make_tools()
    mt._cred_cache = [
        {
            "id": 91,
            "account": "krbtgt",
            "realm": "north.sevenkingdoms.local",
            "credential_text": "deadbeef",
            "task": {
                "display_id": 450,
                "status": "completed",
                "completed": True,
                "command_name": "dcsync",
                "callback": {"display_id": 13},
            },
        },
        {
            "id": 92,
            "account": "unbound",
            "realm": "north.sevenkingdoms.local",
            "credential_text": "x",
        },
    ]
    mt._cred_cache_ts = NOW

    facts = asyncio.run(mt._corroboration_facts(NOW))
    by_predicate = {fact.predicate: fact for fact in facts}
    state = es.EngagementState(
        objective="x",
        graph_facts=facts,
        engagement_id=mt._eng_key(),
        runtime_scope=True,
    )

    assert by_predicate["krbtgt-hash:north.sevenkingdoms.local"].proof_envelope["origin"] == pb.ORIGIN_MYTHIC_CREDENTIAL
    assert by_predicate["creds:unbound@north.sevenkingdoms.local"].proof_envelope == {}
    assert "krbtgt-hash:north.sevenkingdoms.local" in state.satisfied_predicates()
    assert "creds:unbound@north.sevenkingdoms.local" not in state.satisfied_predicates()


def test_corroboration_preserves_bloodhound_lineage_for_graph_derived_facts():
    mt = _make_tools()
    proof = pb.make_runtime_bloodhound_envelope(
        engagement_id=mt._eng_key(),
        callback_id="13",
        task_id="451",
        terminal_status="completed",
        command="execute_assembly",
        ingest_job_id="job-1",
        ingest_status="complete",
        source_artifact_id="file-1",
        source_artifact_sha256="a" * 64,
        verifier_id="test:bloodhound",
        transaction_id="fixture:451",
        verifier_input={"ingest_job_id": "job-1", "task_id": "451"},
        verifier_result={"ingest_status": "complete"},
        captured_at=NOW,
    ).to_dict()
    mt._cred_cache = []
    mt._cred_cache_ts = NOW
    mt._engagement_graph_facts = [
        es.GraphFact("generic-write:gpo:starkwallpaper", "bloodhound:cypher", NOW, 600, proof),
        es.GraphFact("gpo-domain:starkwallpaper:north.sevenkingdoms.local", "bloodhound:cypher", NOW, 600, proof),
    ]

    facts = asyncio.run(mt._corroboration_facts(NOW))
    by_predicate = {fact.predicate: fact for fact in facts}

    assert by_predicate["system:starkwallpaper"].proof_envelope == proof
    assert by_predicate["gpo-domain:starkwallpaper:north.sevenkingdoms.local"].proof_envelope == proof
