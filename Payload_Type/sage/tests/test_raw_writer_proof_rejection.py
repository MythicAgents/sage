"""The raw issue-task ledger writer keeps a constructed, rejected proof candidate and its admission reason.

`_record_engagement_success` builds a runtime task proof envelope from the issued task's lineage and hands it to
the ledger, whose proof boundary admits or rejects it. A rejected candidate used to be dropped before the ledger
saw it, so the row said only "missing proof envelope". These tests drive real `MythicTools` sessions, the real
verifier, envelope builder, proof boundary and `sage.db` store; only Mythic's transport is stubbed where a task is
issued. Missing lineage here is fault-injected; none of these tests reproduces a historical run.

All key material is synthetic.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

import pytest

from ai.langgraph import capabilities, engagement_state, proof_boundary
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract

ENGAGEMENT = "eng-raw-proof-rejection"
DOMAIN = "sevenkingdoms.local"
EFFECT = f"krbtgt-hash:{DOMAIN}"
NTLM = "0123456789abcdef0123456789abcdef"
AES256 = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
DCSYNC_OK = (
    "Object RDN           : krbtgt\n** SAM ACCOUNT **\nCredentials:\n"
    f"  Hash NTLM: {NTLM}\n* Primary:Kerberos-Newer-Keys *\n    Credentials\n"
    f"      aes256_hmac       (4096) : {AES256}\n"
)
DCSYNC_DENIED = "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x00002105 (ERROR_DS_DRA_ACCESS_DENIED)"
TRANSACTION = "tx-raw-writer-1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(mythic_tools_module, "SAGE_ENGAGEMENT_ID", ENGAGEMENT)

    def make(name: str = "raw-writer", request: int = 1) -> MythicTools:
        tools = MythicTools(agent_task_id=name)
        tools.client = object()
        tools.set_request_contract(build_request_contract(
            request_id=f"chat:3:request:{request}", channel_id="channel-3", operation_id="operation-1",
            mode="supervised", autonomous_solve=False,
        ))
        tools._engagement_objective = lambda: ""  # type: ignore[method-assign]
        tools._require_ledger_binding()
        return tools

    return make


@contextlib.contextmanager
def _transaction(transaction_id: str = TRANSACTION):
    token = mythic_tools_module._task_visibility_context.set({"transaction_id": transaction_id})
    try:
        yield
    finally:
        mythic_tools_module._task_visibility_context.reset(token)


def _record(tools: MythicTools, output: str = DCSYNC_OK, *, task_id="45", callback_id="3",
            terminal_status: str = "completed", command: str = "dcsync") -> None:
    """The raw issue-task writer for one verified dcsync result, with the lineage the issue path passes."""
    tools._pending_engagement_hop = ("dcsync", DOMAIN, _now())
    with _transaction():
        tools._record_engagement_success(
            output, task_id=task_id, callback_id=callback_id, terminal_status=terminal_status, command=command,
        )


def _stored(tools: MythicTools) -> list[dict]:
    """Rows as a fresh session in the same operation reads them back from sage.db."""
    reader = MythicTools(agent_task_id="reader")
    reader.client = object()
    reader._require_ledger_binding()
    assert reader._eng_key() == tools._eng_key()
    return [row.payload for row in reader._ledger_snapshot().hop_rows()]


def _achieved(tools: MythicTools) -> set[str]:
    return engagement_state.EngagementState(
        objective="", hops=list(tools._ledger_view().hops), engagement_id=tools._eng_key(), runtime_scope=True,
    ).achieved_effects()


# --- the observed diagnostic loss ----------------------------------------------------------------------------------

def test_a_raw_candidate_without_task_lineage_is_kept_with_its_rejection_reason(session):
    tools = session()
    _record(tools, task_id=None)

    [row] = _stored(tools)
    assert (row["status"], row["pending_reason"]) == ("pending", "proof_missing")
    assert row["evidence"]["proof_admission_reason"] == "missing Mythic task lineage"
    assert row["evidence"]["proof_persistence_state"] == proof_boundary.REJECTED
    candidate = row["proof_envelope"]
    assert candidate["origin"] == proof_boundary.ORIGIN_MYTHIC_TASK
    assert (candidate["task_id"], candidate["callback_id"], candidate["transaction_id"]) == ("", "3", TRANSACTION)
    assert row["evidence"]["proof_hash"] == proof_boundary.ProofEnvelope.from_dict(candidate).hash
    assert EFFECT not in _achieved(tools)


def _runtime_state(tools: MythicTools) -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective="", hops=list(tools._ledger_view().hops), engagement_id=tools._eng_key(), runtime_scope=True,
    )


# --- the control matrix through the raw writer and the store -------------------------------------------------------

def test_a_fully_lineaged_raw_result_is_achieved_on_its_admitted_envelope(session):
    tools = session()
    _record(tools)

    [row] = _stored(tools)
    assert row["status"] == "achieved" and not row.get("pending_reason")
    assert row["effect"] == EFFECT
    assert row["evidence"]["proof_persistence_state"] == proof_boundary.ADMITTED
    assert row["evidence"]["proof_admission_reason"] == "admitted runtime proof"
    proof = row["proof_envelope"]
    assert (proof["task_id"], proof["callback_id"], proof["terminal_status"], proof["command"]) == (
        "45", "3", "completed", "dcsync")
    assert (row["evidence"]["mythic_task_id"], row["evidence"]["callback_id"]) == ("45", "3")
    assert EFFECT in _achieved(tools)
    assert _runtime_state(tools).satisfies_predicate(EFFECT)


@pytest.mark.parametrize(
    ("lineage", "reason"),
    [
        ({"task_id": None}, "missing Mythic task lineage"),
        ({"callback_id": None}, "missing callback lineage"),
        ({"terminal_status": "submitted"}, "task is not terminal-success"),
    ],
    ids=["missing-task", "missing-callback", "nonterminal-task"],
)
def test_each_rejected_raw_candidate_is_kept_pending_with_its_gate_reason(session, lineage, reason):
    tools = session()
    _record(tools, **lineage)

    [row] = _stored(tools)
    assert (row["status"], row["pending_reason"]) == ("pending", "proof_missing")
    assert row["evidence"]["proof_admission_reason"] == reason
    assert row["evidence"]["proof_persistence_state"] == proof_boundary.REJECTED
    assert row["proof_envelope"]["origin"] == proof_boundary.ORIGIN_MYTHIC_TASK
    # The kept candidate is the one the writer built from the lineage it was given; nothing is filled in.
    candidate = proof_boundary.ProofEnvelope.from_dict(row["proof_envelope"])
    assert candidate.task_id == ("" if lineage.get("task_id", "45") is None else "45")
    assert candidate.callback_id == ("" if lineage.get("callback_id", "3") is None else "3")
    assert candidate.terminal_status == lineage.get("terminal_status", "completed")
    # A rejected candidate satisfies no effect or precondition, in the writer's session or a fresh reader's.
    assert EFFECT not in _achieved(tools)
    assert not _runtime_state(tools).satisfies_predicate(EFFECT)
    assert not proof_boundary.admit_runtime_envelope(candidate, current_engagement_id=tools._eng_key()).admitted
    [view_hop] = [hop for hop in tools._ledger_view().hops if hop.effect == EFFECT]
    assert (view_hop.status, view_hop.pending_reason) == ("pending", "proof_missing")
    assert view_hop.evidence["proof_admission_reason"] == reason


def test_a_failed_raw_result_stays_a_request_attempt_without_a_row(session):
    for task_id in ("45", None):
        tools = session(name=f"failed-{task_id}", request=2 if task_id else 3)
        _record(tools, DCSYNC_DENIED, task_id=task_id)
        assert tools._ledger_snapshot().hop_rows() == []
        [attempt] = tools._request_attempt_view()
        assert (attempt.effect, attempt.terminal, attempt.verdict) == (EFFECT, True, "failed")
        assert EFFECT not in _achieved(tools)


def test_missing_authorization_lineage_still_leaves_the_row_without_an_envelope(session, monkeypatch):
    tools = session()
    monkeypatch.setattr(tools, "_evaluation_authorization_enabled", lambda: True)
    _record(tools)

    [row] = _stored(tools)
    assert (row["status"], row["pending_reason"]) == ("pending", "proof_missing")
    assert row["evidence"]["proof_admission_reason"] == "missing proof envelope"
    assert not row.get("proof_envelope")
    assert EFFECT not in _achieved(tools)


# --- the helper keeps its default and its fail-closed exits --------------------------------------------------------

def _helper(tools: MythicTools, **overrides) -> dict:
    kwargs = dict(
        callback_id="3", task_id=None, terminal_status="completed", command="dcsync", transaction_id=TRANSACTION,
        verifier_input={"technique": "dcsync"}, verifier_result={"status": "achieved"},
    )
    kwargs.update(overrides)
    return tools._runtime_task_proof_envelope("engagement_state:dcsync", CAPTURED_AT, **kwargs)


CAPTURED_AT = "2026-11-06T12:00:00+00:00"


def test_the_helper_returns_a_rejected_candidate_only_to_a_caller_that_asks(session):
    tools = session()
    assert _helper(tools) == {}
    assert _helper(tools, retain_rejected=False) == {}
    candidate = _helper(tools, retain_rejected=True)
    assert candidate["task_id"] == "" and candidate["callback_id"] == "3"
    # An admissible envelope is the same envelope whether or not the caller asked for rejected candidates.
    admitted = _helper(tools, task_id="45")
    assert admitted["task_id"] == "45"
    assert _helper(tools, task_id="45", retain_rejected=True) == admitted


def test_the_helper_still_fails_closed_on_authorization_lineage_and_errors(session, monkeypatch):
    tools = session()
    with monkeypatch.context() as patch:
        patch.setattr(tools, "_evaluation_authorization_enabled", lambda: True)
        assert _helper(tools, retain_rejected=True) == {}

    def broken(**_kwargs):
        raise ValueError("construction failed")

    with monkeypatch.context() as patch:
        patch.setattr(proof_boundary, "make_runtime_task_envelope", broken)
        assert _helper(tools, retain_rejected=True) == {}
    with monkeypatch.context() as patch:
        patch.setattr(proof_boundary, "admit_runtime_envelope", broken)
        assert _helper(tools, retain_rejected=True) == {}


def test_the_capability_writer_keeps_the_default(session):
    """The other caller of the helper does not opt in: its rejected candidate never reaches the ledger."""
    tools = session()
    action = capabilities.CapabilityAction(
        name="grant-directory-rights",
        target="domain=lab.local;source=gpo-system-exec:workstation-policy",
        preconditions=["system-exec:gpo:workstation-policy@lab.local", "live-foothold:lab.local"],
        effects=["ds-replication-rights:lab.local"],
    )
    tools.record_capability_result(
        action, {"ds_replication_rights": True}, now=_now(),
        evidence={"callback_id": "3", "transaction_id": TRANSACTION, "terminal_task_status": "completed",
                  "command": "test-command"},
    )
    [row] = _stored(tools)
    assert (row["status"], row["pending_reason"]) == ("pending", "proof_missing")
    assert row["evidence"]["proof_admission_reason"] == "missing proof envelope"
    assert not row.get("proof_envelope")
    assert "ds-replication-rights:lab.local" not in _achieved(tools)


# --- the ledger boundary keeps rejecting a candidate that does not match the claimed row ---------------------------

@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"engagement_id": "another-engagement"}, "proof engagement does not match current engagement"),
        ({"evidence": {"mythic_task_id": "46"}}, "proof task does not match verifier observation"),
        ({"evidence": {"callback_id": "4"}}, "proof callback does not match claimed effect"),
    ],
    ids=["wrong-engagement", "other-task", "other-callback"],
)
def test_a_well_formed_candidate_for_another_row_is_rejected_at_the_ledger(session, change, reason):
    tools = session()
    candidate = _helper(tools, task_id="45", retain_rejected=True)
    evidence = {"source": "issue_task", "mythic_task_id": "45", "callback_id": "3", "transaction_id": TRANSACTION,
                "terminal_task_status": "completed", "command": "dcsync"}
    evidence.update(change.get("evidence", {}))
    state = engagement_state.EngagementState(objective="", engagement_id=tools._eng_key(), runtime_scope=True)
    updated = engagement_state.record_hop_result(
        state, "dcsync", DOMAIN, "achieved", evidence, _now(), proof_envelope=candidate,
        require_admissible_proof=True, engagement_id=change.get("engagement_id", tools._eng_key()),
    )
    [hop] = updated.hops
    assert (hop.status, hop.pending_reason) == ("pending", "proof_missing")
    assert hop.evidence["proof_admission_reason"] == reason
    assert EFFECT not in updated.achieved_effects()


def test_an_artifact_with_no_envelope_is_never_admitted_by_fallback(session):
    tools = session()
    state = engagement_state.EngagementState(objective="", engagement_id=tools._eng_key(), runtime_scope=True)
    evidence = {"source": "issue_task", "mythic_task_id": "45", "callback_id": "3", "transaction_id": TRANSACTION,
                "terminal_task_status": "completed", "command": "dcsync", "artifact_present": True}
    updated = engagement_state.record_hop_result(
        state, "dcsync", DOMAIN, "achieved", evidence, _now(), proof_envelope={},
        require_admissible_proof=True, engagement_id=tools._eng_key(),
    )
    [hop] = updated.hops
    assert (hop.status, hop.pending_reason) == ("pending", "proof_missing")
    assert hop.evidence["proof_admission_reason"] == "missing proof envelope"
    assert EFFECT not in updated.achieved_effects()


# --- the assembled issue path --------------------------------------------------------------------------------------

def test_an_issued_dcsync_task_records_its_own_lineage_through_the_raw_writer(session, monkeypatch):
    """Only Mythic's task transport is stubbed: the classifier arms the hop, the issue path fills the task slot,
    and the classifier, verifier, envelope builder, proof boundary and store are the production code."""
    from ai.langgraph.turn_authority import authority_from_request_contract

    issued: list = []

    async def issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True,
                         timeout=None):
        issued.append((command_name, callback_display_id))
        return {"display_id": 4501}

    async def waitfor(mythic, task_display_id, timeout=None):
        assert task_display_id == 4501
        return DCSYNC_OK

    monkeypatch.setattr(mythic_tools_module.mythic, "issue_task", issue_task)
    monkeypatch.setattr(mythic_tools_module.mythic, "waitfor_for_task_output", waitfor)

    tools = session()
    contract = build_request_contract(
        request_id="chat:3:request:9", channel_id="channel-3", operation_id="operation-1",
        mode="auto", autonomous_solve=True,
    )
    tools.set_request_contract(contract)
    tools._turn_authority = authority_from_request_contract(contract)
    assert tools._pending_engagement_hop is None

    asyncio.run(tools.issue_task_and_waitfor_task_output(
        "dcsync", {"Domain": DOMAIN, "User": "krbtgt", "DC": f"kingslanding.{DOMAIN}"}, 3,
    ))

    assert issued == [("dcsync", 3)]
    [row] = _stored(tools)
    assert row["technique"] == "dcsync" and row["status"] == "achieved"
    assert row["evidence"]["proof_persistence_state"] == proof_boundary.ADMITTED
    proof = row["proof_envelope"]
    assert (proof["task_id"], proof["callback_id"], proof["terminal_status"], proof["command"]) == (
        "4501", "3", "completed", "dcsync")
    assert EFFECT in _achieved(tools)
