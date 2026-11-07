"""Phase 2: a completion-function-error task proves credential material only through an independent review.

Mythic task 18 (2026-11-06) returned an account secret, then Apollo's completion function failed and Mythic overwrote
the status. Group A keeps such a task pending under every writer. This module covers the review that may promote it:
locate a candidate in the task's actual output, validate it by credential type and against the task's target
account and realm, persist one review record per task/effect/output/rule, and admit a material-only proof at the proof
boundary. A material gain satisfies an "obtain the secret" objective; every working-credential gate still needs a
working contribution.

Only Mythic's transport is stubbed. The account, realm and key material are synthetic.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ai.langgraph import autonomous_controller, capabilities, credential_review, engagement_ledger, engagement_state
from ai.langgraph import graph_reconciler, proof_boundary, state_reconcile, task_evidence
from tests.esl_fixtures import AGENT_PROCESSED, stub_mythic_operation
from tests.test_engagement_ledger_phase2_unified_proof import (  # noqa: F401 - fixtures are used by name
    ACCOUNT,
    CALLBACK,
    COMPLETION_ERROR,
    DCSYNC_OUTPUT,
    DCSYNC_PARAMS,
    NTLM,
    REALM,
    SUCCESS,
    TASK,
    TYPED,
    WORKING,
    _capability,
    _capability_issue,
    _events,
    _observations,
    _reader,
    _typed_rows,
    mythic_issue,
    session,
)

MATERIAL = f"cred-material:hash:{ACCOUNT}@{REALM}"
OBJECTIVE = f"Obtain the credentials for {ACCOUNT}@{REALM}"


def _state(tools, objective: str = OBJECTIVE) -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective=objective, hops=list(tools._ledger_view().hops), engagement_id=tools._eng_key(), runtime_scope=True,
    )


def _reviews(row) -> list[dict]:
    return [review for contribution in row.payload["contributions"]
            for review in (contribution.get("reviews") or {}).values()]


def _review_observation(row, task: str = str(TASK)) -> dict:
    contribution = next(c for c in row.payload["contributions"] if c["task_id"] == task)
    slots = contribution["writers"]["review"]
    return slots.get("admitted") or slots.get("rejected")


def _history_task(display_id: int = TASK, params: dict | None = None, **record) -> dict:
    return {"display_id": display_id, "id": display_id, "operation_id": 1, "command_name": "dcsync",
            "original_params": json.dumps(params or DCSYNC_PARAMS), "operator": {"username": "operator"},
            "callback": {"display_id": CALLBACK, "host": "WS01"}, **record}


_COMPLETION_ERROR_ROW = {k: v for k, v in COMPLETION_ERROR.items() if k not in {"callback", "responses"}}


# --- the task 18 shape, through the issue path ----------------------------------------------------------------------

def test_task_18_gains_material_only_through_a_persisted_review(session, mythic_issue, mythic_task_records):
    mythic_task_records[TASK] = COMPLETION_ERROR
    tools = session()
    _capability_issue(tools)

    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "achieved"
    assert row.payload["satisfied_effects"] == [MATERIAL]
    observed = _observations(row)["18"]
    assert observed["review"] == ("admitted", task_evidence.POSTPROCESSING_ERROR_WITH_OUTPUT)
    assert observed["raw"][0] == observed["capability"][0] == "rejected"  # neither writer credits output alone

    [review] = _reviews(row)
    assert review["verdict"] == credential_review.VALID
    assert (review["reviewer_kind"], review["rule_version"]) == ("deterministic", credential_review.RULE_VERSION)
    assert (review["candidate"]["type"], review["candidate"]["account"], review["candidate"]["realm"]) == (
        "ntlm", ACCOUNT, REALM)
    assert review["canonical_effect"] == TYPED and review["task_id"] == str(TASK)
    start, end = review["location"]["start"], review["location"]["end"]
    assert DCSYNC_OUTPUT[start:end] == NTLM  # the citation re-reads to the candidate in Mythic's output
    # No secret value in the review record, the review observation or its proof envelope.
    assert NTLM not in json.dumps(review)
    assert NTLM not in json.dumps(_review_observation(row))

    state = _state(_reader())
    assert engagement_state.objective_effects_complete(state)
    assert not state.satisfies_predicate(WORKING)


def test_without_the_review_the_completion_error_task_stays_pending(session, mythic_issue, mythic_task_records,
                                                                    monkeypatch):
    """The guard above can fail: with no review the task 18 shape proves nothing."""
    monkeypatch.setattr(credential_review, "review_output",
                        lambda *args, **kwargs: credential_review.ReviewOutcome.not_applicable("disabled"))
    mythic_task_records[TASK] = COMPLETION_ERROR
    tools = session()
    _capability_issue(tools)
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending"
    assert not engagement_state.objective_effects_complete(_state(_reader()))


def test_the_boundary_admits_the_review_proof_only_for_its_one_material_effect(session, mythic_issue,
                                                                               mythic_task_records):
    mythic_task_records[TASK] = COMPLETION_ERROR
    tools = session()
    _capability_issue(tools)
    [row] = _typed_rows(_reader())
    observation = _review_observation(row)
    envelope = proof_boundary.ProofEnvelope.from_dict(observation["proof_envelope"])
    lineage = {"current_engagement_id": tools._eng_key(), "expected_task_id": str(TASK),
               "expected_callback_id": str(CALLBACK)}

    assert proof_boundary.admit_task_observation(envelope, claimed_effects=[MATERIAL], **lineage).admitted
    for claimed in ([WORKING], [MATERIAL, WORKING], [], None, [f"cred-material:key:{ACCOUNT}@{REALM}"]):
        assert not proof_boundary.admit_task_observation(envelope, claimed_effects=claimed, **lineage).admitted
    # The plain runtime rule never admits it: the task is not terminal success.
    assert not proof_boundary.admit_runtime_envelope(envelope, current_engagement_id=tools._eng_key()).admitted
    # A tampered review commitment, a different task or a different classification is rejected.
    data = observation["proof_envelope"]
    for change in ({"metadata": dict(data["metadata"], review_id="0" * 64)},
                   {"metadata": dict(data["metadata"], review_output_sha256="1" * 64)},
                   {"metadata": dict(data["metadata"], review_verdict="rejected")},
                   {"metadata": dict(data["metadata"], task_classification=task_evidence.TERMINAL_SUCCESS)},
                   {"task_id": "19"}):
        tampered = proof_boundary.ProofEnvelope.from_dict(dict(data, **change))
        assert not proof_boundary.admit_task_observation(
            tampered, claimed_effects=[MATERIAL], current_engagement_id=tools._eng_key()).admitted


def test_a_row_whose_review_proof_is_relabelled_as_working_proves_nothing(session, mythic_issue,
                                                                          mythic_task_records):
    mythic_task_records[TASK] = COMPLETION_ERROR
    tools = session()
    _capability_issue(tools)
    [hop] = [hop for hop in tools._ledger_view().hops if hop.canonical_effect == TYPED]
    assert engagement_state._hop_has_admissible_runtime_proof(hop, tools._eng_key())
    relabelled = engagement_state.Hop(**{**hop.__dict__, "satisfied_effects": [WORKING, MATERIAL]})
    assert not engagement_state._hop_has_admissible_runtime_proof(relabelled, tools._eng_key())
    assert not engagement_ledger.hop_proof_admission(
        {**relabelled.__dict__, "proof_envelope": hop.proof_envelope}, tools._eng_key())[0]
    state = engagement_state.EngagementState(objective=OBJECTIVE, hops=[relabelled], engagement_id=tools._eng_key(),
                                             runtime_scope=True)
    assert state.achieved_effects() == set()


def test_a_later_terminal_success_task_adds_the_working_credential(session, mythic_issue, mythic_task_records):
    mythic_task_records[TASK] = COMPLETION_ERROR
    mythic_task_records[30] = SUCCESS
    tools = session()
    _capability_issue(tools)
    asyncio.run(tools._observe_task_record(30, CALLBACK))
    _capability(tools, 30)

    [row] = _typed_rows(_reader())
    assert [c["task_id"] for c in row.payload["contributions"]] == ["18", "30"]
    assert set(row.payload["satisfied_effects"]) == {WORKING, MATERIAL}
    assert row.payload["effect"] == WORKING  # the headline proof is the working contribution
    assert _state(_reader()).satisfies_predicate(WORKING)


# --- the cited reconciliation the live protocol uses ----------------------------------------------------------------

@pytest.mark.parametrize("apply", [False, True])
def test_reconcile_of_task_18_attaches_material_once_and_reuses_the_review(session, monkeypatch, apply):
    tools = session()
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [_history_task(**_COMPLETION_ERROR_ROW)]
    records["outputs"] = {TASK: DCSYNC_OUTPUT}
    if apply:  # Mythic's credential store is not part of this boundary
        async def no_import(client, materials, task_id, apply=False):
            return [], []
        monkeypatch.setattr(state_reconcile, "_import_reconciled_credentials", no_import)

    asyncio.run(tools._reconcile_operation([str(TASK)], apply=apply))  # /state reconcile 18 [apply]
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "achieved" and row.payload["satisfied_effects"] == [MATERIAL]
    assert _observations(row)["18"]["reconcile"][0] == "rejected"
    assert _observations(row)["18"]["review"][0] == "admitted"

    calls = []
    real = credential_review.review_output
    monkeypatch.setattr(credential_review, "review_output", lambda *a, **k: calls.append(1) or real(*a, **k))
    events = _events(tools)
    reloaded = session("reloaded", request=2)  # a new session loads the stored ledger
    asyncio.run(reloaded._reconcile_operation([str(TASK)], apply=apply))  # the cited replay
    assert _events(tools) == events and calls == []  # the stored review is reused, nothing is regraded
    [again] = _typed_rows(_reader())
    assert again.number == row.number and len(_reviews(again)) == 1


def test_a_changed_output_is_a_new_review_version_not_a_silent_regrade(session, monkeypatch):
    tools = session()
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [_history_task(**_COMPLETION_ERROR_ROW)]
    records["outputs"] = {TASK: DCSYNC_OUTPUT}
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    records["outputs"] = {TASK: DCSYNC_OUTPUT + "\n[*] trailing response\n"}
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    reviews = _reviews(row)
    assert len(reviews) == 2 and len({review["input_sha256"] for review in reviews}) == 2
    assert row.payload["status"] == "achieved"


# --- negative classes: located but not bound, not reviewable, nothing to review --------------------------------------

_WRONG_ACCOUNT = DCSYNC_OUTPUT.replace(f"SAM Username         : {ACCOUNT}", "SAM Username         : other.user")
_WRONG_REALM = "[DC] 'south.example.local' will be the domain\n" + DCSYNC_OUTPUT


@pytest.mark.parametrize(("output", "reason"), [
    (_WRONG_ACCOUNT, "account"),
    (_WRONG_REALM, "realm"),
])
def test_a_candidate_bound_to_another_account_or_realm_stays_pending(session, monkeypatch, output, reason):
    tools = session()
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = [_history_task(**_COMPLETION_ERROR_ROW)]
    records["outputs"] = {TASK: output}
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending"
    [review] = _reviews(row)
    assert review["verdict"] == credential_review.REJECTED and reason in review["reason"]
    assert _observations(row)["18"]["review"][0] == "rejected"
    assert not engagement_state.objective_effects_complete(_state(_reader()))


@pytest.mark.parametrize("record", [
    SUCCESS,                                              # terminal success: the working path, no review
    dict(SUCCESS, status="error"),                        # agent error
    dict(COMPLETION_ERROR, responses=[]),                 # completion error without output: unknown
])
def test_only_a_completion_error_task_with_output_is_reviewed(session, mythic_issue, mythic_task_records, record):
    mythic_task_records[TASK] = record
    tools = session()
    _capability_issue(tools)
    [row] = _typed_rows(_reader())
    assert "review" not in _observations(row)["18"]
    assert _reviews(row) == []


# --- the review rule itself: type validators, binding, citation -----------------------------------------------------

def _review(output: str, account: str = ACCOUNT, realm: str = REALM):
    return credential_review.review_output(output, task_id=str(TASK), canonical_effect=f"account-secret:{account}@{realm}",
                                           account=account, realm=realm)


@pytest.mark.parametrize(("output", "verdict", "kind"), [
    (DCSYNC_OUTPUT, credential_review.VALID, "ntlm"),
    (DCSYNC_OUTPUT.replace(NTLM, NTLM[:31]), credential_review.REJECTED, ""),        # malformed width
    (DCSYNC_OUTPUT.replace(NTLM, "31d6cfe0d16ae931b73c59d7e0c089c0"), credential_review.REJECTED, ""),  # blank NT
    (DCSYNC_OUTPUT.replace(NTLM, "0" * 32), credential_review.REJECTED, ""),         # all zero
    (f"[DC] 'north.example.local' will be the domain\n[DC] 'north\\{ACCOUNT}' will be the user account\n"
     + DCSYNC_OUTPUT, credential_review.VALID, "ntlm"),                               # matching realm and NETBIOS user
    (f"SAM Username         : {ACCOUNT}\n* Primary:Kerberos-Newer-Keys *\n    aes256_hmac       (4096) : "
     + "ab" * 32 + "\n", credential_review.VALID, "aes256"),
    (f"NORTH\\{ACCOUNT}:1104:aad3b435b51404eeaad3b435b51404ee:{NTLM}:::\n", credential_review.VALID, "ntlm"),
    (f"SOUTH\\{ACCOUNT}:1104:aad3b435b51404eeaad3b435b51404ee:{NTLM}:::\n", credential_review.REJECTED, ""),
    (f"Hash NTLM: {NTLM}\n", credential_review.REJECTED, ""),                         # no account names the secret
    ("The operation completed successfully.\n", credential_review.NOT_APPLICABLE, ""),  # nothing to locate
])
def test_the_review_validates_by_type_and_binds_to_the_target(output, verdict, kind):
    outcome = _review(output)
    assert outcome.record["verdict"] == verdict
    if kind:
        assert outcome.record["candidate"]["type"] == kind
        assert outcome.material_kind in {"hash", "key"}
    for value in (NTLM, "ab" * 32):
        assert value not in json.dumps(outcome.record)


def test_a_citation_outside_the_output_or_on_other_text_is_rejected():
    outcome = _review(DCSYNC_OUTPUT)
    location = outcome.record["location"]
    assert credential_review.check_citation(DCSYNC_OUTPUT, location, "ntlm")[0]
    for bad in ({**location, "end": len(DCSYNC_OUTPUT) + 10}, {**location, "start": 0, "end": 12},
                {**location, "start": location["end"], "end": location["start"]}):
        assert not credential_review.check_citation(DCSYNC_OUTPUT, bad, "ntlm")[0]


def test_the_value_hash_is_one_way_and_salted_by_rule():
    outcome = _review(DCSYNC_OUTPUT)
    digest = outcome.record["candidate"]["value_sha256"]
    assert len(digest) == 64 and digest != __import__("hashlib").sha256(NTLM.encode()).hexdigest()


# --- consumers: material satisfies the objective, never a working-credential gate ------------------------------------

def _material_only_state(tools, account: str = ACCOUNT) -> engagement_state.EngagementState:
    return _state(tools, objective=f"Obtain the credentials for {account}@{REALM}")


def test_material_only_unlocks_no_working_credential_consumer(session, mythic_issue, mythic_task_records):
    mythic_task_records[TASK] = COMPLETION_ERROR
    tools = session()
    _capability_issue(tools)
    state = _material_only_state(_reader())
    assert MATERIAL in state.achieved_effects() and WORKING not in state.satisfied_predicates()

    # The autonomous controller's route-discovery admission (auto and supervised with Autonomous Solve).
    context = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context", target=f"domain={REALM};account={ACCOUNT};callback={CALLBACK}",
        preconditions=[WORKING], effects=[f"account-kerberos-context:{ACCOUNT}@{REALM}"])
    controller = object.__new__(autonomous_controller.AutonomousController)
    assert not controller._candidate_admissible(state, context)
    # The worker still offers the DCSync: its working-credential effect is not achieved.
    dcsync = capabilities.CapabilityAction(name="dcsync-account", target=f"domain={REALM};account={ACCOUNT}",
                                           preconditions=[], effects=[WORKING])
    assert not tools._capability_action_effects_achieved(dcsync, state.achieved_effects())
    # BloodHound reconciliation does not treat the account as controlled.
    assert f"{ACCOUNT}@{REALM}" not in graph_reconciler.controlled_principals_from_state(state)
    # A later no-key DCSync is still a recorded attempt: material is not a verified working credential to keep.
    assert _reader()._prior_verified_credential_hop("dcsync-user", f"north\\{ACCOUNT}@{REALM}") is None


def test_the_same_consumers_unlock_with_a_working_contribution(session, mythic_issue, mythic_task_records):
    """Positive control for the test above, so it cannot pass for a reason unrelated to proof strength."""
    mythic_task_records[TASK] = SUCCESS
    tools = session()
    _capability_issue(tools)
    state = _material_only_state(_reader())
    context = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context", target=f"domain={REALM};account={ACCOUNT};callback={CALLBACK}",
        preconditions=[WORKING], effects=[f"account-kerberos-context:{ACCOUNT}@{REALM}"])
    controller = object.__new__(autonomous_controller.AutonomousController)
    assert controller._candidate_admissible(state, context)
    dcsync = capabilities.CapabilityAction(name="dcsync-account", target=f"domain={REALM};account={ACCOUNT}",
                                           preconditions=[], effects=[WORKING])
    assert tools._capability_action_effects_achieved(dcsync, state.achieved_effects())
    assert engagement_state.objective_effects_complete(state)


@pytest.mark.parametrize(("achieved", "complete"), [
    ({MATERIAL}, True),
    ({WORKING}, True),
    ({f"cred-material:hash:other.user@{REALM}"}, False),
    ({f"cred-material:hash:{ACCOUNT}@south.example.local"}, False),
    ({f"cred-material:dump:ws01.{REALM}"}, False),
    (set(), False),
])
def test_objective_matching_accepts_material_for_the_named_account_only(achieved, complete):
    assert engagement_state._credential_effect_satisfied(ACCOUNT, REALM, achieved) is complete


@pytest.mark.parametrize(("achieved", "satisfied"), [
    ({f"krbtgt-hash:{REALM}"}, True),
    ({f"cred-material:hash:krbtgt@{REALM}"}, True),
    ({f"cred-material:key:krbtgt@{REALM}"}, True),
    ({f"cred-material:hash:krbtgt@south.example.local"}, False),
    ({f"cred-material:hash:{ACCOUNT}@{REALM}"}, False),
])
def test_the_krbtgt_objective_witness_accepts_material(achieved, satisfied):
    assert engagement_state._effect_satisfied(f"krbtgt-hash:{REALM}", achieved) is satisfied


def test_krbtgt_material_does_not_offer_ticket_forgery():
    """`forge-golden-ticket` needs `krbtgt-hash:`; material for krbtgt satisfies only the objective witness."""
    def state(effect: str) -> engagement_state.EngagementState:
        hop = engagement_state.Hop(
            id="h", technique="account-secret", target=f"krbtgt@{REALM}", effect=effect, status="achieved",
            evidence={}, preconditions=[], source="test", timestamp="2026-11-07T00:00:00+00:00",
            satisfied_effects=[effect])
        return engagement_state.EngagementState(objective="", hops=[hop], engagement_id="e", runtime_scope=False)

    def forge(effect: str) -> list:
        return [a for a in capabilities.actions_from_state(state(effect)) if a.name == "forge-golden-ticket"]

    assert forge(f"krbtgt-hash:{REALM}")  # positive control: a working krbtgt credential offers forgery
    assert not forge(f"cred-material:hash:krbtgt@{REALM}")
    assert not forge(f"cred-material:key:krbtgt@{REALM}")


# --- found in the cold review ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("plaintext_task", [10, 25])
def test_a_review_headline_keeps_the_rows_other_admitted_material(session, monkeypatch, plaintext_task):
    """Task 18 is reviewed to hash material; a terminal-success, non-replication task puts a plaintext credential for
    the same account in Mythic's credential store. With task 25 the review proof is the row's headline; every reader
    must still keep both material effects, as group A kept the plaintext before the review existed."""
    tools = session()
    records = stub_mythic_operation(monkeypatch)
    success = {k: v for k, v in SUCCESS.items() if k != "callback"}
    lsass = _history_task(plaintext_task, params={"command": "sekurlsa::logonpasswords"}, **success)
    lsass["command_name"] = "mimikatz"
    records["tasks"] = [_history_task(**_COMPLETION_ERROR_ROW), lsass]
    records["outputs"] = {TASK: DCSYNC_OUTPUT, plaintext_task: "Authentication Id : 0 ; 999\n"}
    records["credentials"] = [{"id": 7, "account": ACCOUNT, "realm": REALM, "type": "plaintext", "operation_id": 1,
                               "credential_text": "synthetic", "task": dict(lsass)}]
    asyncio.run(tools._reconcile_operation([str(TASK), str(plaintext_task)]))

    plaintext = f"cred-material:plaintext:{ACCOUNT}@{REALM}"
    [row] = _typed_rows(_reader())
    assert set(row.payload["satisfied_effects"]) == {MATERIAL, plaintext}
    review_headline = row.payload["proof_envelope"]["metadata"].get("proof_form") == "credential_review_material"
    assert review_headline is (plaintext_task > TASK)
    state = _state(_reader())
    assert {MATERIAL, plaintext} <= state.achieved_effects() and WORKING not in state.achieved_effects()
    assert engagement_ledger.hop_proof_admission(dict(row.payload), tools._eng_key())[0]


def test_without_an_object_header_each_secret_binds_to_the_nearest_account_line():
    output = (f"SAM Username         : other.user\nCredentials:\n  Hash NTLM: {'1' * 31}2\n"
              f"SAM Username         : {ACCOUNT}\nCredentials:\n  Hash NTLM: {NTLM}\n")
    outcome = _review(output)
    assert outcome.record["verdict"] == credential_review.VALID
    assert output[outcome.record["location"]["start"]:outcome.record["location"]["end"]] == NTLM
    assert _review(output.replace(f"SAM Username         : {ACCOUNT}", "SAM Username         : third.user"),
                   ).record["verdict"] == credential_review.REJECTED


def test_a_rejected_review_stores_only_well_formed_bounded_names():
    output = DCSYNC_OUTPUT.replace(f"SAM Username         : {ACCOUNT}", "SAM Username         : " + "x|" * 150)
    record = _review(output).record
    assert record["verdict"] == credential_review.REJECTED
    assert record["candidate"]["account"] == ""
