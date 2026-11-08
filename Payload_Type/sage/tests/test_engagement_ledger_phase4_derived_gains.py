"""Phase 4 remaining sources, checkpoint 4 part 1: derived gains.

A derived gain is a logical implication over admitted premises, computed by the one evaluator the runtime gate and
the ledger reconciler share. A persisted ``derived_rule`` row recomputes its named rule over premises resolved to
admitted rows; it never credits an effect after its premises stop holding. These exercise the evaluator, the proof
form, the gate exclusion, and the reconciler persistence.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from ai.langgraph import effect_rules as er, engagement_state as es, mythic_tools, proof_boundary as pb

NOW = "2026-11-08T12:00:00+00:00"
FOREST = "north.local"


# --- Unit: the shared evaluator --------------------------------------------------------------------------------

def test_the_three_rules_fire_on_their_premises():
    da = er.evaluate({f"da:{FOREST}", f"live-foothold:{FOREST}"})
    assert [(d.rule_id, d.output_effect) for d in da] == [
        (er.RULE_DA_REPLICATION, f"ds-replication-rights:{FOREST}")]
    ctx = er.evaluate({f"kerberos-context:{FOREST}@callback:7", "live-callback:7"})
    assert [(d.rule_id, d.output_effect) for d in ctx] == [
        (er.RULE_KERBEROS_CONTEXT, f"kerberos-context:{FOREST}")]
    gpo = er.evaluate({"system:{abc}", "gpo-domain:{abc}:corp.local"})
    assert [(d.rule_id, d.output_effect) for d in gpo] == [
        (er.RULE_GPO_REPLICATION, "ds-replication-rights:corp.local")]


def test_a_domain_admin_without_a_usable_context_derives_nothing():
    assert er.evaluate({f"da:{FOREST}"}) == []            # no live foothold or context
    assert f"ds-replication-rights:{FOREST}" not in er.expand({f"da:{FOREST}"})


@pytest.mark.parametrize("rule, premises, conditions", [
    (er.RULE_DA_REPLICATION, ("creds:x@y",), (f"live-foothold:{FOREST}",)),        # foreign premise
    (er.RULE_DA_REPLICATION, (f"da:{FOREST}",), ("live-foothold:other.local",)),   # condition domain mismatch
    ("no-such-rule", (f"da:{FOREST}",), (f"live-foothold:{FOREST}",)),             # unknown rule
    (er.RULE_GPO_REPLICATION, ("system:{abc}",), ()),                              # wrong arity
])
def test_apply_rule_does_not_fire_on_bad_inputs(rule, premises, conditions):
    assert er.apply_rule(rule, premises, conditions) == ""


def test_expand_adds_only_the_output_effects():
    base = {f"da:{FOREST}", f"live-foothold:{FOREST}", "unrelated:thing"}
    assert er.expand(base) == base | {f"ds-replication-rights:{FOREST}"}


# --- Unit: the derived_rule proof form -------------------------------------------------------------------------

def _envelope(**over):
    kwargs = dict(
        engagement_id="eng", rule_id=er.RULE_DA_REPLICATION, rule_version="v1",
        premises=(f"da:{FOREST}",), conditions=(f"live-foothold:{FOREST}",),
        premise_rows=[{"predicate": f"da:{FOREST}", "row_id": "5", "revision": "2", "proof_commitment": "a" * 64}],
        output_effect=f"ds-replication-rights:{FOREST}", captured_at=NOW,
    )
    kwargs.update(over)
    return pb.make_derived_rule_envelope(**kwargs)


def test_a_sound_derivation_is_admitted():
    assert pb.admit_runtime_envelope(_envelope(), current_engagement_id="eng").admitted


@pytest.mark.parametrize("mutate, reason", [
    (lambda m: m.update(output_effect="da:evil.local"), "result commitment"),
    (lambda m: m.update(rule_id="no-such-rule"), "unknown derivation rule"),
])
def test_a_tampered_derivation_is_rejected(mutate, reason):
    env = _envelope()
    metadata = dict(env.metadata)
    mutate(metadata)
    tampered = dataclasses.replace(env, metadata=metadata)
    admission = pb.admit_runtime_envelope(tampered, current_engagement_id="eng")
    assert not admission.admitted and reason in admission.reason


def test_a_premise_with_no_admitted_row_is_rejected():
    # built honestly (commitment matches), but the premise names no resolvable row
    env = _envelope(premise_rows=[{"predicate": f"da:{FOREST}", "row_id": "", "revision": "", "proof_commitment": ""}])
    admission = pb.admit_runtime_envelope(env, current_engagement_id="eng")
    assert not admission.admitted and "no admitted row" in admission.reason


def test_a_derivation_whose_rule_does_not_recompute_its_output_is_rejected():
    # built honestly (every commitment valid), but the premise/condition do not imply the stated output: a domain
    # admin on north.local with a foothold in a DIFFERENT domain does not derive north.local's replication rights.
    env = _envelope(conditions=("live-foothold:other.local",))
    admission = pb.admit_runtime_envelope(env, current_engagement_id="eng")
    assert not admission.admitted and "recompute" in admission.reason


def test_a_cycle_is_rejected():
    env = _envelope(premises=(f"ds-replication-rights:{FOREST}",), premise_rows=[
        {"predicate": f"ds-replication-rights:{FOREST}", "row_id": "5", "revision": "2", "proof_commitment": "a" * 64}])
    # the rule cannot recompute its own output from itself, so admission fails before the cycle guard; either way no gain
    assert not pb.admit_runtime_envelope(env, current_engagement_id="eng").admitted


def test_the_wrong_engagement_is_rejected():
    assert not pb.admit_runtime_envelope(_envelope(), current_engagement_id="other").admitted


# --- Unit: the gate excludes a persisted derivation -----------------------------------------------------------

def _derived_hop(effect, premise):
    env = pb.make_derived_rule_envelope(
        engagement_id="eng", rule_id=er.RULE_DA_REPLICATION, rule_version="v1", premises=(premise,),
        conditions=(f"live-foothold:{FOREST}",),
        premise_rows=[{"predicate": premise, "row_id": "5", "revision": "2", "proof_commitment": "a" * 64}],
        output_effect=effect, captured_at=NOW)
    return es.Hop(id=f"derived:{effect}", technique="derived:x", target=effect, effect=effect, status="achieved",
                  evidence={"proof_envelope": env.to_dict()}, preconditions=[], satisfied_effects=[effect],
                  source="derivation", timestamp=NOW, proof_envelope=env.to_dict())


def test_a_persisted_derivation_is_not_a_premise_for_itself_or_the_gate():
    hop = _derived_hop(f"ds-replication-rights:{FOREST}", f"da:{FOREST}")
    state = es.EngagementState(objective="", hops=[hop], engagement_id="eng", runtime_scope=False)
    # the derived row is excluded from the achieved-effect premises (it is a record, not a premise)
    assert f"ds-replication-rights:{FOREST}" not in state.achieved_effects()
    assert es._is_derived_hop(hop) is True


# --- Assembled: the reconciler persists a derivation ----------------------------------------------------------

from tests.esl_fixtures import seed_ledger  # noqa: E402
from tests.test_engagement_wiring import _foothold, _make_tools, _proof_hop  # noqa: E402


def _reconcile_derived(mt):
    view = mt._ledger_view()
    state = es.EngagementState(objective="", hops=list(view.hops), engagement_id=mt._eng_key(), runtime_scope=True)
    return asyncio.run(mt._derived_gain_hops(state, NOW, produced={}, mutation_ids={}))


def test_a_domain_admin_row_plus_a_live_foothold_persists_the_replication_derivation():
    mt = _make_tools()
    seed_ledger(mt, hops=[_proof_hop(f"da:{FOREST}", 100, technique="golden-ticket", target=FOREST)])
    mt._engagement_footholds = [_foothold(forest=FOREST)]

    out = _reconcile_derived(mt)
    derived = [h for h in out.hops if h.effect == f"ds-replication-rights:{FOREST}" and es._is_derived_hop(h)]
    assert len(derived) == 1
    proof = derived[0].proof_envelope
    assert proof["origin"] == pb.ORIGIN_DERIVED_RULE
    assert proof["metadata"]["rule_id"] == er.RULE_DA_REPLICATION


def test_no_derivation_without_an_admitted_premise_row():
    mt = _make_tools()
    mt._engagement_footholds = [_foothold(forest=FOREST)]  # a live foothold but no da: premise row
    out = _reconcile_derived(mt)
    assert not any(es._is_derived_hop(h) for h in out.hops)


def test_a_real_replication_row_is_not_shadowed_by_a_derivation():
    mt = _make_tools()
    seed_ledger(mt, hops=[
        _proof_hop(f"da:{FOREST}", 100, technique="golden-ticket", target=FOREST),
        _proof_hop(f"ds-replication-rights:{FOREST}", 101, technique="dcsync-rights-grant", target=FOREST)])
    mt._engagement_footholds = [_foothold(forest=FOREST)]
    out = _reconcile_derived(mt)
    assert not any(es._is_derived_hop(h) for h in out.hops)  # the real row already proves it


def test_the_derived_effect_lapses_when_its_live_foothold_is_gone():
    mt = _make_tools()
    seed_ledger(mt, hops=[_proof_hop(f"da:{FOREST}", 100, technique="golden-ticket", target=FOREST)])
    mt._engagement_footholds = []  # no live foothold -> da: is held but not usable
    out = _reconcile_derived(mt)
    assert not any(es._is_derived_hop(h) for h in out.hops)
    # and the gate itself does not satisfy the derived effect from the durable da: row alone
    view = mt._ledger_view()
    state = es.EngagementState(objective="", footholds=[], hops=list(view.hops), engagement_id=mt._eng_key(),
                               runtime_scope=True)
    assert f"ds-replication-rights:{FOREST}" not in state.satisfied_predicates()

