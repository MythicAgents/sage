"""Requested account DCSync targets and repeat verification in ordinary supervised chat.

Through the assembled native-chat consumer: the real handoff tool, catalog producer, brief admission, catalog
binding, approval card and MythicTools.execute_capability, with Mythic network I/O recorded instead of sent. The
scripted Supervisor proves wiring, not model choice; the model seam (the Supervisor selecting an offered candidate)
is the plan's live check. Ledger state is synthetic and labelled.
"""

from __future__ import annotations

import asyncio

import pytest

from ai.langgraph import catalog_binding
from tests.esl_fixtures import seed_ledger
from tests.test_engagement_wiring import _proof_hop
from tests.test_supervised_catalog_step_binding import (
    _call, _cards, _client, _isolated, _listed, _run, _select,
)


def _accounts(run) -> set[str]:
    """The dcsync-account candidate targets the Supervisor was shown on the first model call."""
    return {target for _ref, procedure, target, _callback in _listed(run.fake.inputs[0])
            if procedure == "dcsync-account"}


def _dcsync_account_brief(ref: str = "cand-0000000000000000", **fields) -> dict:
    return _call("transfer_to_Mythic_Operator", {
        "handoff_title": "dcsync requested accounts", "handoff_instruction": "requested accounts",
        "allowed_capabilities": ["dcsync-account"], "callback_display_id": 14, "targets": ["child.lab.local"],
        "expected_result": "Account credential material.", "candidate_ref": ref, **fields}, 1)


# --- (a) accounts the operator names, absent from the graph/objective, become candidates --------------------------

def test_named_bare_accounts_in_one_eligible_domain_are_offered(monkeypatch, tmp_path):
    mt, split = _isolated(monkeypatch, tmp_path / "op")
    seed_ledger(mt, graph_facts=[{"predicate": "ds-replication-rights:child.lab.local", "source": "fixture",
                                  "timestamp": "2026-11-14T13:00:00+00:00", "ttl_seconds": 86400}])
    prompt = "DCSync jon.snow and sansa.stark on callback 14."
    run = asyncio.run(_run(mt, split, [_select("dcsync-account", contains="jon.snow")], prompt=prompt))

    accounts = _accounts(run)
    assert any("account=jon.snow" in target for target in accounts), accounts
    assert any("account=sansa.stark" in target for target in accounts), accounts
    assert run.issued, "the selected candidate issued no Mythic task"


def test_proving_one_named_account_leaves_the_other_eligible(monkeypatch, tmp_path):
    mt, split = _isolated(monkeypatch, tmp_path / "op")
    seed_ledger(mt, graph_facts=[{"predicate": "ds-replication-rights:child.lab.local", "source": "fixture",
                                  "timestamp": "2026-11-14T13:00:00+00:00", "ttl_seconds": 86400}])
    # jon.snow already proved; the request names both. The satisfied-domain filter must not suppress sansa.stark.
    seed_ledger(mt, hops=[_proof_hop("creds:jon.snow@child.lab.local", "70", callback_id="14",
                                     technique="capability:dcsync-account", target="domain=child.lab.local;account=jon.snow")])
    prompt = "DCSync jon.snow and sansa.stark"
    run = asyncio.run(_run(mt, split, [_select("dcsync-account", contains="sansa.stark")], prompt=prompt))

    accounts = _accounts(run)
    assert any("account=sansa.stark" in target for target in accounts), accounts
    assert not any("account=jon.snow" in target for target in accounts), "a proved account is not re-offered"


# --- (b) ambiguous scope: a clarification, zero cards or tasks ----------------------------------------------------

def test_bare_names_in_more_than_one_eligible_domain_ask_to_qualify(monkeypatch, tmp_path):
    mt, split = _isolated(monkeypatch, tmp_path / "op")
    seed_ledger(mt, graph_facts=[{"predicate": f"ds-replication-rights:{domain}", "source": "fixture",
                                  "timestamp": "2026-11-14T13:00:00+00:00", "ttl_seconds": 86400}
                                 for domain in ("child.lab.local", "lab.local")])
    prompt = "DCSync alice and bob"
    run = asyncio.run(_run(mt, split, [_dcsync_account_brief()], prompt=prompt))

    assert not _accounts(run), "an ambiguous bare name is not offered as a candidate"
    assert run.issued == [] and run.all_cards == []
    [terminal] = run.terminals
    assert "more than one domain" in terminal["content"], terminal["content"]


# --- (c) an ordinary request for an already-proved effect ends Blocked citing the proof --------------------------

def test_ordinary_request_for_a_proved_account_cites_its_proof(monkeypatch, tmp_path):
    mt, split = _isolated(monkeypatch, tmp_path / "op")
    seed_ledger(mt, graph_facts=[{"predicate": "ds-replication-rights:child.lab.local", "source": "fixture",
                                  "timestamp": "2026-11-14T13:00:00+00:00", "ttl_seconds": 86400}])
    seed_ledger(mt, hops=[_proof_hop("creds:administrator@child.lab.local", "88", callback_id="14",
                                     technique="capability:dcsync-account", target="domain=child.lab.local;account=administrator")])
    prompt = "DCSync administrator"
    run = asyncio.run(_run(mt, split, [_dcsync_account_brief()], prompt=prompt))

    assert run.issued == [] and run.all_cards == []
    [terminal] = run.terminals
    assert "creds:administrator@child.lab.local is already proved" in terminal["content"], terminal["content"]
    assert "task 88" in terminal["content"], terminal["content"]


# --- (d) an explicit re-verify re-offers the proved account and issues a task ------------------------------------

def test_reverify_offers_the_proved_account_and_issues_a_task(monkeypatch, tmp_path):
    mt, split = _isolated(monkeypatch, tmp_path / "op")
    seed_ledger(mt, graph_facts=[{"predicate": "ds-replication-rights:child.lab.local", "source": "fixture",
                                  "timestamp": "2026-11-14T13:00:00+00:00", "ttl_seconds": 86400}])
    seed_ledger(mt, hops=[_proof_hop("creds:administrator@child.lab.local", "88", callback_id="14",
                                     technique="capability:dcsync-account", target="domain=child.lab.local;account=administrator")])
    prompt = "Re-verify administrator with DCSync"
    run = asyncio.run(_run(mt, split, [_select("dcsync-account", contains="administrator")], prompt=prompt))

    accounts = _accounts(run)
    assert any("account=administrator" in target for target in accounts), accounts
    assert run.issued, "the re-verify issued no fresh Mythic task"


# --- (e) change 3: the refresh bit is pinned; a card may not add or drop it --------------------------------------

def _candidate(refresh: bool) -> catalog_binding.Candidate:
    inputs = {"callback_id": "14"}
    if refresh:
        inputs["force_refresh"] = True
    return catalog_binding.Candidate(
        ref="cand-x", procedure="dcsync-account", target="domain=child.lab.local;account=administrator",
        callback="14", effects=("creds:administrator@child.lab.local",),
        action={"name": "dcsync-account", "target": "domain=child.lab.local;account=administrator",
                "intent": {"capability": "dcsync-account"}},
        inputs=inputs)


@pytest.mark.parametrize("candidate_refresh, call_refresh, ok", [
    (True, True, True), (False, False, True), (True, False, False), (False, True, False)])
def test_call_matches_pins_the_refresh_bit(candidate_refresh, call_refresh, ok):
    candidate = _candidate(candidate_refresh)
    inputs = {"callback_id": "14"}
    if call_refresh:
        inputs["force_refresh"] = True
    args = {"action": dict(candidate.action), "inputs": inputs}
    error = catalog_binding.call_matches(args, candidate)
    assert (error == "") is ok, error
