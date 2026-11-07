"""A pending GPO setup row joins a later SYSTEM-execution proof only on its own admissible proof.

`engagement_state._reconcile_proven_gpo_setup_hops` re-records a pending `gpo-abuse` setup row as achieved when an
admitted achieved row proves `system-exec:gpo:<gpo>@<domain>` in the setup row's transaction, read from the setup
row's stored envelope. The raw issue-task writer keeps a rejected proof candidate so the row shows its real
admission reason; that candidate is diagnostic evidence only and must never associate its task with the later
proof. A valid setup row with admitted proof, still pending while its effect is awaited, must keep reconciling.

The raw-writer cases drive real `MythicTools` sessions, the real verifier, envelope builder, proof boundary,
reconciler and `sage.db` store. Missing or non-terminal task lineage is fault-injected and reproduces no
historical run. All identities are synthetic.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from ai.langgraph import engagement_state, proof_boundary
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph.mythic_tools import MythicTools
from ai.langgraph.request_contract import build_request_contract
from tests.esl_fixtures import observe_task

ENGAGEMENT = "eng-gpo-setup-guard"
GPO = "kingslanding"
DOMAIN = "north.sevenkingdoms.local"
EXEC_EFFECT = f"system-exec:gpo:{GPO}@{DOMAIN}"
SETUP_EFFECT = f"system:{GPO}"
RIGHTS = f"ds-replication-rights:{DOMAIN}"
TRANSACTION = "capability-exec:gpo-kingslanding"
EXEC_TECHNIQUE = "capability:gpo-controlled-system-exec"
EXEC_TARGET = f"gpo={GPO};domain={DOMAIN}"
SETUP_TASK, EXEC_TASK, CALLBACK = "77", "78", "3"
CAPTURED_AT = "2026-11-06T12:00:00+00:00"

# SharpGPOAbuse's "modified; wait for refresh" output: setup, recorded pending / awaiting_effect.
SETUP_OUTPUT = (
    f"[+] Domain = {DOMAIN}\r\n"
    f"[+] Domain Controller = winterfell.{DOMAIN}\r\n"
    "[+] Distinguished Name = CN=Policies,CN=System,DC=north,DC=sevenkingdoms,DC=local\r\n"
    f"[+] GUID of \"{GPO}\" is: {{3F1C08F5-D531-4D99-83B3-5CCF0A3981EF}}\r\n"
    f"[+] Creating file \\\\{DOMAIN}\\SysVol\\{DOMAIN}\\Policies\\"
    "{3F1C08F5-D531-4D99-83B3-5CCF0A3981EF}\\Machine\\Preferences\\ScheduledTasks\\ScheduledTasks.xml\r\n"
    "[+] versionNumber attribute changed successfully\r\n"
    "[+] The version number in GPT.ini was increased successfully.\r\n"
    "[+] The GPO was modified to include a new immediate task. Wait for the GPO refresh cycle.\n"
    "[+] Done!\r\n"
)
# Observed SYSTEM execution: the verifier asks for achieved, so an unadmitted candidate stays proof_missing.
SYSTEM_OUTPUT = "whoami\r\nnt authority\\system\r\n"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(mythic_tools_module, "SAGE_ENGAGEMENT_ID", ENGAGEMENT)

    def make(name: str = "gpo-setup-guard") -> MythicTools:
        tools = MythicTools(agent_task_id=name)
        tools.client = object()
        tools.set_request_contract(build_request_contract(
            request_id="chat:3:request:1", channel_id="channel-3", operation_id="operation-1",
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


def _gpo_domain_fact(engagement_id: str) -> engagement_state.GraphFact:
    """The GPO-to-domain mapping that turns `system:<gpo>` into replication rights, with payload-collected proof."""
    proof = proof_boundary.make_runtime_bloodhound_envelope(
        engagement_id=engagement_id, callback_id=CALLBACK, task_id="60", terminal_status="completed",
        command="download", ingest_job_id="bh-job-60", ingest_status="complete", source_artifact_id="artifact-60",
        source_artifact_sha256="a" * 64, verifier_id="test:bloodhound-ingest", captured_at=CAPTURED_AT,
        transaction_id="fixture:60", verifier_input={"task_id": "60"}, verifier_result={"verified": True},
    ).to_dict()
    return engagement_state.GraphFact(f"gpo-domain:{GPO}:{DOMAIN}", "bloodhound:cypher", CAPTURED_AT, 3600, proof)


def _state(engagement_id: str, hops, *, runtime_scope: bool = True) -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective="", hops=list(hops), graph_facts=[_gpo_domain_fact(engagement_id)],
        engagement_id=engagement_id, runtime_scope=runtime_scope,
    )


def _task_envelope(engagement_id: str, *, task_id=SETUP_TASK, transaction_id=TRANSACTION,
                   command="execute_assembly") -> dict:
    return proof_boundary.make_runtime_task_envelope(
        engagement_id=engagement_id, callback_id=CALLBACK, task_id=task_id, terminal_status="completed",
        command=command, verifier_id="engagement_state:gpo-abuse", captured_at=CAPTURED_AT,
        transaction_id=transaction_id, verifier_input={"task_id": task_id}, verifier_result={"status": "pending"},
    ).to_dict()


def _record_execution(state: engagement_state.EngagementState, *, task_id=EXEC_TASK,
                      transaction_id=TRANSACTION) -> engagement_state.EngagementState:
    """An admitted capability proof of SYSTEM execution through the GPO, through the ledger boundary."""
    proof = _task_envelope(state.engagement_id, task_id=task_id, transaction_id=transaction_id, command="shell")
    evidence = {"source": "capability", "mythic_task_id": task_id, "callback_id": CALLBACK,
                "transaction_id": transaction_id, "terminal_task_status": "completed", "command": "shell"}
    return engagement_state.record_effect_result(
        state, EXEC_TECHNIQUE, EXEC_TARGET, EXEC_EFFECT, "achieved", evidence, _now(),
        satisfied_effects=[EXEC_EFFECT], proof_envelope=proof, require_admissible_proof=True,
        engagement_id=state.engagement_id,
    )


def _record_unrelated(state: engagement_state.EngagementState) -> engagement_state.EngagementState:
    """A later ledger write of another row, as end-of-turn and `/state` reconciliation make: the reconciler reruns."""
    proof = _task_envelope(state.engagement_id, task_id="90", transaction_id="fixture:90", command="ls")
    evidence = {"source": "test", "mythic_task_id": "90", "callback_id": CALLBACK, "transaction_id": "fixture:90",
                "terminal_task_status": "completed", "command": "ls"}
    return engagement_state.record_effect_result(
        state, "test-unrelated", "share", "share-read:share", "achieved", evidence, _now(),
        satisfied_effects=["share-read:share"], proof_envelope=proof, require_admissible_proof=True,
        engagement_id=state.engagement_id,
    )


def _hop(state: engagement_state.EngagementState, technique: str) -> engagement_state.Hop:
    [hop] = [hop for hop in state.hops if hop.technique == technique]
    return hop


def _assert_no_effect_beyond(state: engagement_state.EngagementState, control: engagement_state.EngagementState):
    """The setup row adds nothing to what the admitted execution row alone already proves."""
    assert state.achieved_effects() == control.achieved_effects()
    assert state.satisfied_predicates() == control.satisfied_predicates()
    assert SETUP_EFFECT not in state.achieved_effects()
    assert EXEC_EFFECT in state.achieved_effects()


def _control(engagement_id: str, *, runtime_scope: bool = True) -> engagement_state.EngagementState:
    return _record_execution(_state(engagement_id, [], runtime_scope=runtime_scope))


# --- the raw writer's retained rejected candidate through the store -----------------------------------------------

def _raw_setup(tools: MythicTools, output: str, *, task_id=SETUP_TASK, callback_id=CALLBACK,
               record: dict | None = None, records: dict | None = None) -> None:
    """The raw issue-task writer for one SharpGPOAbuse task, inside the capability's transaction.

    ``record`` is served as Mythic's record of the task through ``records`` (the ``mythic_task_records`` stub) for
    the shared classifier; by default Mythic reports the task finished."""
    if record is not None and records is not None:
        records[int(task_id)] = record
    observe_task(tools, task_id, callback_id)
    tools._pending_engagement_hop = ("gpo-abuse", GPO, _now())
    with _transaction():
        tools._record_engagement_success(
            output, task_id=task_id, callback_id=callback_id, command="execute_assembly",
        )


def _commit_execution(tools: MythicTools) -> None:
    before = _state(tools._eng_key(), tools._ledger_view().hops)
    after = _record_execution(before)
    assert tools._ledger_commit_hops(before.hops, after.hops, what="test execution")


def _stored_state(tools: MythicTools) -> engagement_state.EngagementState:
    """The rows a fresh session in the same operation reads back from sage.db."""
    reader = MythicTools(agent_task_id="reader")
    reader.client = object()
    reader._require_ledger_binding()
    assert reader._eng_key() == tools._eng_key()
    return _state(reader._eng_key(), reader._ledger_view().hops)


_FAULTS = {
    "nonterminal-task": ({"record": {"completed": False, "status": "submitted"}}, "task is not terminal-success"),
    "missing-task": ({"task_id": None}, "missing Mythic task lineage"),
    "missing-callback": ({"callback_id": None}, "missing callback lineage"),
}
_OUTPUTS = {"setup-output": (SETUP_OUTPUT, "awaiting_effect"), "system-output": (SYSTEM_OUTPUT, "proof_missing")}


@pytest.mark.parametrize("order", ["setup-first", "execution-first"])
@pytest.mark.parametrize("output", list(_OUTPUTS))
@pytest.mark.parametrize("fault", list(_FAULTS))
def test_a_retained_rejected_setup_candidate_never_joins_a_valid_execution(session, mythic_task_records, fault,
                                                                           output, order):
    tools = session()
    lineage, reason = _FAULTS[fault]
    lineage = dict(lineage, records=mythic_task_records)
    text, pending_reason = _OUTPUTS[output]
    if order == "execution-first":
        _commit_execution(tools)
    _raw_setup(tools, text, **lineage)
    if order == "setup-first":
        _commit_execution(tools)

    stored = _stored_state(tools)
    setup = _hop(stored, "gpo-abuse")
    assert (setup.status, setup.pending_reason) == ("pending", pending_reason)
    assert setup.evidence["proof_admission_reason"] == reason
    assert setup.evidence["proof_persistence_state"] == proof_boundary.REJECTED
    assert "reconciled_from_hop" not in setup.evidence
    # The setup row keeps its own candidate, not the execution task's envelope.
    candidate = proof_boundary.ProofEnvelope.from_dict(setup.proof_envelope)
    assert candidate.task_id == ("" if lineage.get("task_id", SETUP_TASK) is None else SETUP_TASK)
    assert candidate.callback_id == ("" if lineage.get("callback_id", CALLBACK) is None else CALLBACK)
    assert candidate.transaction_id == TRANSACTION
    assert setup.evidence["proof_hash"] == candidate.hash
    assert _hop(stored, EXEC_TECHNIQUE).status == "achieved"
    _assert_no_effect_beyond(stored, _control(tools._eng_key()))


@pytest.mark.parametrize("order", ["setup-first", "execution-first"])
def test_a_valid_pending_setup_still_joins_its_execution_through_the_store(session, order):
    tools = session()
    if order == "execution-first":
        _commit_execution(tools)
    _raw_setup(tools, SETUP_OUTPUT)
    before = _stored_state(tools) if order == "setup-first" else None
    if order == "setup-first":
        pending = _hop(before, "gpo-abuse")
        assert (pending.status, pending.pending_reason) == ("pending", "awaiting_effect")
        assert pending.evidence["proof_persistence_state"] == proof_boundary.ADMITTED
        _commit_execution(tools)

    stored = _stored_state(tools)
    setup = _hop(stored, "gpo-abuse")
    assert setup.status == "achieved"
    assert str(setup.evidence["reconciled_setup_task_id"]) == SETUP_TASK
    assert str(setup.evidence["mythic_task_id"]) == EXEC_TASK
    # The setup task is linked by its ID; no task output enters the reconciled row.
    assert "reconciled_setup_preview" not in setup.evidence and "result_preview" not in setup.evidence
    if before is not None:
        assert (setup.id, setup.number, setup.row_id) == (pending.id, pending.number, pending.row_id)
    assert {SETUP_EFFECT, EXEC_EFFECT} <= stored.achieved_effects()
    assert RIGHTS in stored.satisfied_predicates()


# --- the setup guard at the reconciler, in and out of runtime scope ------------------------------------------------

def _admitted_evidence(engagement_id: str, envelope: dict) -> dict:
    evidence, admission = proof_boundary.attach_proof(
        {"source": "issue_task", "verify_verdict": "partial"},
        proof_boundary.ProofEnvelope.from_dict(envelope),
        current_engagement_id=engagement_id,
    )
    assert admission.admitted
    return evidence


def _setup_hop(evidence: dict, envelope: dict, *, storage: str, corrections=None) -> engagement_state.Hop:
    evidence = dict(evidence)
    if storage == "evidence":
        evidence["proof_envelope"] = dict(envelope)
        top_level = {}
    else:
        evidence.pop("proof_envelope", None)
        top_level = dict(envelope)
    return engagement_state.Hop(
        id=f"gpo-abuse:{GPO}", technique="gpo-abuse", target=GPO, effect=SETUP_EFFECT, status="pending",
        evidence=evidence, preconditions=[], satisfied_effects=[SETUP_EFFECT], source="issue_task",
        timestamp=CAPTURED_AT, proof_envelope=top_level, pending_reason="awaiting_effect",
        number=4, row_id=11, revision=2, corrections=dict(corrections or {}),
    )


def _without_marker(evidence: dict) -> dict:
    """An older row, written before the ledger persisted its admission verdict."""
    evidence = dict(evidence)
    evidence.pop("proof_persistence_state", None)
    evidence.pop("proof_admission_reason", None)
    return evidence


def _ledger_rejected(engagement_id: str, change: dict) -> tuple[dict, dict]:
    """A structurally admissible candidate the ledger rejected for a lineage mismatch with the claimed row."""
    envelope = _task_envelope(engagement_id)
    evidence = {"source": "issue_task", "mythic_task_id": SETUP_TASK, "callback_id": CALLBACK,
                "transaction_id": TRANSACTION, "terminal_task_status": "completed", "command": "execute_assembly",
                "verify_verdict": "partial", **change}
    recorded = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="", engagement_id=engagement_id, runtime_scope=True),
        "gpo-abuse", GPO, "pending", evidence, _now(), proof_envelope=envelope, engagement_id=engagement_id,
    )
    hop = _hop(recorded, "gpo-abuse")
    assert hop.evidence["proof_persistence_state"] == proof_boundary.REJECTED
    # The boundary rewrote the evidence lineage from the candidate, so the envelope alone now admits:
    # only the persisted verdict still says this candidate was rejected for this row.
    assert engagement_state._hop_has_admissible_runtime_proof(hop, engagement_id)
    return hop.evidence, dict(hop.proof_envelope)


def _invalid_setup(case: str, engagement_id: str) -> tuple[dict, dict, dict]:
    """(evidence, envelope, corrections) for one setup row whose proof must not be used for the join."""
    valid = _task_envelope(engagement_id)
    admitted = _admitted_evidence(engagement_id, valid)
    if case == "wrong-engagement":
        envelope = _task_envelope("another-engagement")
        return _without_marker(admitted), envelope, {}
    if case == "malformed":
        return _without_marker(admitted), {**valid, "schema": "not-a-proof"}, {}
    if case == "absent":
        evidence = _without_marker(admitted)
        evidence.pop("proof_hash", None)
        return evidence, {}, {}
    if case == "task-mismatch":
        return (*_ledger_rejected(engagement_id, {"mythic_task_id": "76"}), {})
    if case == "callback-mismatch":
        return (*_ledger_rejected(engagement_id, {"callback_id": "4"}), {})
    if case == "transaction-mismatch":
        return (*_ledger_rejected(engagement_id, {"transaction_id": "capability-exec:another"}), {})
    if case == "rejected-marker":
        return {**admitted, "proof_persistence_state": proof_boundary.REJECTED}, valid, {}
    if case == "legacy-marker":
        return {**admitted, "proof_persistence_state": proof_boundary.LEGACY_UNVERIFIED}, valid, {}
    if case == "retracted":
        return admitted, valid, {"retracted": True}
    if case == "no-longer-holds":
        return admitted, valid, {"no_longer_holds": {"effective": True}}
    raise AssertionError(case)


_INVALID = ["wrong-engagement", "malformed", "absent", "task-mismatch", "callback-mismatch", "transaction-mismatch",
            "rejected-marker", "legacy-marker", "retracted", "no-longer-holds"]


def _reconcile(state: engagement_state.EngagementState, trigger: str) -> engagement_state.EngagementState:
    """Run the reconciler on the execution row's own write, or on a later write once both rows are stored."""
    if trigger == "execution-write":
        return _record_execution(state)
    return _record_unrelated(replace(state, hops=list(_control_hops(state)) + list(state.hops)))


def _control_hops(state: engagement_state.EngagementState):
    return _control(state.engagement_id, runtime_scope=state.runtime_scope).hops


@pytest.mark.parametrize("trigger", ["execution-write", "later-write"])
@pytest.mark.parametrize("storage", ["top-level", "evidence"])
@pytest.mark.parametrize("runtime_scope", [True, False], ids=["runtime", "non-runtime"])
@pytest.mark.parametrize("case", _INVALID)
def test_invalid_setup_proof_never_joins_a_valid_execution(case, runtime_scope, storage, trigger):
    evidence, envelope, corrections = _invalid_setup(case, ENGAGEMENT)
    setup = _setup_hop(evidence, envelope, storage=storage, corrections=corrections)
    updated = _reconcile(_state(ENGAGEMENT, [setup], runtime_scope=runtime_scope), trigger)

    kept = _hop(updated, "gpo-abuse")
    assert kept == setup  # unchanged: status, reason, envelope, verdict, corrections and row identity
    assert _hop(updated, EXEC_TECHNIQUE).status == "achieved"
    control = _control(ENGAGEMENT, runtime_scope=runtime_scope)
    if trigger == "later-write":
        control = _record_unrelated(control)
    _assert_no_effect_beyond(updated, control)


@pytest.mark.parametrize("trigger", ["execution-write", "later-write"])
@pytest.mark.parametrize("storage", ["top-level", "evidence"])
@pytest.mark.parametrize("runtime_scope", [True, False], ids=["runtime", "non-runtime"])
@pytest.mark.parametrize("marker", ["admitted", "no-marker"])
def test_valid_setup_proof_still_joins_a_matching_execution(marker, runtime_scope, storage, trigger):
    envelope = _task_envelope(ENGAGEMENT)
    evidence = _admitted_evidence(ENGAGEMENT, envelope)
    if marker == "no-marker":
        evidence = _without_marker(evidence)
    setup = _setup_hop(evidence, envelope, storage=storage)
    updated = _reconcile(_state(ENGAGEMENT, [setup], runtime_scope=runtime_scope), trigger)

    promoted = _hop(updated, "gpo-abuse")
    assert promoted.status == "achieved"
    assert (promoted.id, promoted.number, promoted.row_id, promoted.revision) == (setup.id, 4, 11, 2)
    assert promoted.evidence["reconciled_setup_task_id"] == evidence.get("mythic_task_id")
    assert {SETUP_EFFECT, EXEC_EFFECT} <= updated.achieved_effects()
    assert RIGHTS in updated.satisfied_predicates()


@pytest.mark.parametrize("runtime_scope", [True, False], ids=["runtime", "non-runtime"])
def test_a_rejected_setup_keeps_its_diagnostics_beside_an_unrelated_proof(runtime_scope):
    """A proof in another transaction stays a negative and leaves a rejected setup row unchanged. The other-GPO,
    other-transaction and unproven-execution controls for valid setup rows live in `test_engagement_wiring.py`."""
    envelope = _task_envelope(ENGAGEMENT)
    setup = _setup_hop({**_admitted_evidence(ENGAGEMENT, envelope),
                        "proof_persistence_state": proof_boundary.REJECTED}, envelope, storage="top-level")
    elsewhere = _record_execution(_state(ENGAGEMENT, [setup], runtime_scope=runtime_scope),
                                  transaction_id="capability-exec:another")
    assert _hop(elsewhere, "gpo-abuse") == setup


def test_historical_previews_never_enter_a_reconciled_setup_row():
    """Rows stored before previews were removed keep them in history; a reconciled setup row copies neither."""
    envelope = _task_envelope(ENGAGEMENT)
    setup = _setup_hop(dict(_admitted_evidence(ENGAGEMENT, envelope), result_preview="SETUP-OUTPUT"), envelope,
                       storage="top-level")
    state = _state(ENGAGEMENT, [setup])
    proof = _task_envelope(ENGAGEMENT, task_id=EXEC_TASK, command="shell")
    evidence = {"source": "capability", "mythic_task_id": EXEC_TASK, "callback_id": CALLBACK,
                "transaction_id": TRANSACTION, "terminal_task_status": "completed", "command": "shell",
                "result_preview": "EXECUTION-OUTPUT"}
    updated = engagement_state.record_effect_result(
        state, EXEC_TECHNIQUE, EXEC_TARGET, EXEC_EFFECT, "achieved", evidence, _now(),
        satisfied_effects=[EXEC_EFFECT], proof_envelope=proof, require_admissible_proof=True,
        engagement_id=ENGAGEMENT,
    )
    assert _hop(updated, EXEC_TECHNIQUE).evidence.get("result_preview") == "EXECUTION-OUTPUT"  # the stored proof
    promoted = _hop(updated, "gpo-abuse")
    assert promoted.status == "achieved"
    assert "result_preview" not in promoted.evidence and "reconciled_setup_preview" not in promoted.evidence
