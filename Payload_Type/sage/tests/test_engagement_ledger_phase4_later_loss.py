"""Phase 4 remaining sources, checkpoint 4: later loss (D9 markers).

A gain's achieved history and original proof are preserved; a conclusive later observation appends a "no longer
holds" marker so nothing downstream relies on the row until a newer proof re-establishes it. Timeouts, partial
enumerations and generic denials are not conclusive. Operator corrections are never displaced.
"""

from __future__ import annotations

import asyncio

import pytest

from ai.langgraph import effect_observations as eo, engagement_state as es, engagement_store

# Reuse the directory-source assembled harness and constants.
from tests.test_engagement_ledger_phase4_directory_sources import (  # noqa: E402
    BOTH, GRANT_PARAMS, GRANT_OK, HEADER, READ_PARAMS, _achieved, _json, _reconcile, directory_mythic,  # noqa: F401
    session,  # noqa: F401
)

EFFECT = "ds-replication-rights:north.sevenkingdoms.local"
NO_RIGHTS = HEADER + "[*] enumerated, no extended rights present\n"


def _stored_row(tools, effect):
    for r in tools._ledger_snapshot().hop_rows():
        payload = r.payload or {}
        effects = set(payload.get("satisfied_effects") or [])
        effects.add(payload.get("effect"))
        if effect in effects:
            return r
    return None


def _no_longer_holds(tools, effect) -> bool:
    row = _stored_row(tools, effect)
    return bool(row and (row.invalidation or {}).get("effective"))


# --- Directory contradiction --------------------------------------------------------------------------------

def test_a_later_complete_read_without_the_right_retires_the_directory_gain(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    tools = session()
    _reconcile(tools)
    assert EFFECT in _achieved(tools)

    directory_mythic(22, READ_PARAMS, NO_RIGHTS)  # a later complete read: the right is gone
    _reconcile(tools)
    assert _no_longer_holds(tools, EFFECT)
    assert EFFECT not in es.EngagementState(
        objective="", hops=list(tools._ledger_view().hops), engagement_id=tools._eng_key(),
        runtime_scope=True).achieved_effects()


def test_an_incomplete_later_read_is_not_conclusive_loss(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    tools = session()
    _reconcile(tools)

    # a read of the exact object that returns no replication ACEs but is NOT a complete DACL: the absence is not
    # conclusive, so the gain stands.
    directory_mythic(22, READ_PARAMS, _json([], complete=False))
    _reconcile(tools)
    assert not _no_longer_holds(tools, EFFECT)
    assert EFFECT in _achieved(tools)


def test_a_newer_proof_re_establishes_a_retired_directory_gain(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    tools = session()
    _reconcile(tools)
    directory_mythic(22, READ_PARAMS, NO_RIGHTS)
    _reconcile(tools)
    assert _no_longer_holds(tools, EFFECT)

    # the right is granted again and a later read proves it: the automatic marker lapses on the new achievement
    directory_mythic(23, GRANT_PARAMS, GRANT_OK)
    directory_mythic(24, READ_PARAMS, HEADER + BOTH)
    _reconcile(tools)
    assert not _no_longer_holds(tools, EFFECT)
    assert EFFECT in _achieved(tools)


def test_an_operator_correction_is_not_displaced_by_the_automatic_marker(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    tools = session()
    _reconcile(tools)
    row = _stored_row(tools, EFFECT)
    tools._ledger_store().invalidate(tools._require_ledger_binding(), [str(row.number)], reason="operator judged stale",
                                     actor=engagement_store.SAGE_ACTOR)
    directory_mythic(22, READ_PARAMS, NO_RIGHTS)
    _reconcile(tools)  # the automatic marker must not overwrite the operator's

    stored = next(r for r in tools._ledger_snapshot().hop_rows() if r.identity == row.identity)
    assert (stored.invalidation or {}).get("scope") == "operator"


def test_reconcile_converges_without_duplicate_markers(session, directory_mythic):
    directory_mythic(20, GRANT_PARAMS, GRANT_OK)
    directory_mythic(21, READ_PARAMS, HEADER + BOTH)
    directory_mythic(22, READ_PARAMS, NO_RIGHTS)
    tools = session()
    _reconcile(tools)
    from tests.test_engagement_ledger_phase4_observation_joins import _events
    before = len(_events(tools))
    _reconcile(tools)
    assert len(_events(tools)) == before and _no_longer_holds(tools, EFFECT)


# --- Derived-premise loss and re-establishment --------------------------------------------------------------

from tests.esl_fixtures import seed_ledger  # noqa: E402
from tests.test_engagement_wiring import _foothold, _make_tools, _proof_hop  # noqa: E402

NOW = "2026-11-08T12:00:00+00:00"
FOREST = "north.local"
REPL = f"ds-replication-rights:{FOREST}"


def test_a_derived_row_is_retired_when_its_premise_stops_holding():
    mt = _make_tools()
    seed_ledger(mt, hops=[_proof_hop(f"da:{FOREST}", 100, technique="golden-ticket", target=FOREST)])
    mt._engagement_footholds = [_foothold(forest=FOREST)]
    view = mt._ledger_view()
    state = es.EngagementState(objective="", hops=list(view.hops), engagement_id=mt._eng_key(), runtime_scope=True)
    out = asyncio.run(mt._derived_gain_hops(state, NOW, produced={}, mutation_ids={}))
    mt._ledger_commit_hops(view.hops, out.hops, what="derived")
    assert REPL in {h.effect for h in mt._ledger_view().hops if h.status == "achieved"}

    mt._engagement_footholds = []  # the live foothold is gone: the derivation no longer fires
    asyncio.run(mt._loss_marker_hops(NOW))
    assert _no_longer_holds(mt, REPL)


import json  # noqa: E402

from tests.test_engagement_ledger_phase4_session_sources import (  # noqa: E402
    DIR_LISTING, KERBEROS_EFFECT, KERBEROS_IMPORT, MAKE_TOKEN_OUT, session_mythic,  # noqa: F401
)

_MT_PARAMS = json.dumps({"username": "samwell.tarly", "domain": "north.sevenkingdoms.local"})


def _resource_effect(tools):
    return next((h.effect for h in tools._ledger_view().hops
                 if h.status == "achieved" and h.effect.startswith("authenticated-session:")), None)


def test_a_later_context_change_retires_the_session_gain(session, session_mythic):
    session_mythic(20, "execute_assembly", "Rubeus.exe ptt", KERBEROS_IMPORT)
    session_mythic(21, "ls", "\\\\winterfell\\c$", DIR_LISTING)
    tools = session()
    _reconcile(tools)
    assert KERBEROS_EFFECT in _achieved(tools)

    # a later ticket import for a DIFFERENT account on the same callback replaces the prior session
    session_mythic(22, "execute_assembly", "Rubeus.exe ptt", KERBEROS_IMPORT.replace("samwell.tarly", "jon.snow"))
    _reconcile(tools)
    assert _no_longer_holds(tools, KERBEROS_EFFECT)


def test_a_same_account_re_set_does_not_retire_a_resource_session(session, session_mythic):
    # the resource-session effect target is account@domain@resource; a same-account make_token must not retire it.
    session_mythic(20, "make_token", _MT_PARAMS, MAKE_TOKEN_OUT)
    session_mythic(21, "ls", "\\\\winterfell\\c$", DIR_LISTING)
    tools = session()
    _reconcile(tools)
    effect = _resource_effect(tools)
    assert effect is not None

    session_mythic(25, "make_token", _MT_PARAMS, MAKE_TOKEN_OUT)  # same account, same callback
    _reconcile(tools)
    assert not _no_longer_holds(tools, effect)


def test_a_different_account_context_retires_a_resource_session(session, session_mythic):
    session_mythic(20, "make_token", _MT_PARAMS, MAKE_TOKEN_OUT)
    session_mythic(21, "ls", "\\\\winterfell\\c$", DIR_LISTING)
    tools = session()
    _reconcile(tools)
    effect = _resource_effect(tools)
    assert effect is not None

    other = json.dumps({"username": "jon.snow", "domain": "north.sevenkingdoms.local"})
    session_mythic(25, "make_token", other, MAKE_TOKEN_OUT.replace("samwell.tarly", "jon.snow"))
    _reconcile(tools)
    assert _no_longer_holds(tools, effect)


def test_a_derived_row_with_a_surviving_premise_is_not_retired():
    mt = _make_tools()
    seed_ledger(mt, hops=[_proof_hop(f"da:{FOREST}", 100, technique="golden-ticket", target=FOREST)])
    mt._engagement_footholds = [_foothold(forest=FOREST)]
    view = mt._ledger_view()
    state = es.EngagementState(objective="", hops=list(view.hops), engagement_id=mt._eng_key(), runtime_scope=True)
    out = asyncio.run(mt._derived_gain_hops(state, NOW, produced={}, mutation_ids={}))
    mt._ledger_commit_hops(view.hops, out.hops, what="derived")

    asyncio.run(mt._loss_marker_hops(NOW))  # premise and foothold still hold
    assert not _no_longer_holds(mt, REPL)
