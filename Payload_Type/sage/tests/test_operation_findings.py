from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib

import pytest

from sage_chat.operation_findings import (
    EvidencePointer,
    FindingCandidate,
    FindingState,
    current_findings_view,
    list_notification_events,
    reconcile_findings,
    stable_finding_id,
)
from sage_chat.operation_memory import OperationMemoryStore, SourceRecord


def _evidence(number: int, *, callback: str = "4") -> EvidencePointer:
    digest = hashlib.sha256(f"evidence-{number}".encode()).hexdigest()
    return EvidencePointer.build(
        record_class="task_output",
        source_record_id=str(number),
        revision_sha256=digest,
        callback_display_id=callback,
        task_display_id=str(number + 100),
        task_output_id=str(number),
    )


async def _ingest_candidate_evidence(
    store: OperationMemoryStore,
    operation_id: str,
    candidates: list[FindingCandidate] | tuple[FindingCandidate, ...],
) -> None:
    pointers = {
        (pointer.record_class, pointer.source_record_id, pointer.revision_sha256)
        for candidate in candidates
        for pointer in candidate.evidence
    }
    records = []
    for record_class, source_record_id, revision_sha256 in sorted(pointers):
        content = f"evidence-{source_record_id}"
        record = SourceRecord.build(
            operation_id=operation_id,
            record_class=record_class,
            source_record_id=source_record_id,
            observed_at_utc="2026-08-01T00:00:00Z",
            content=content,
            callback_display_id="4",
            task_display_id=str(int(source_record_id) + 100),
            task_output_id=source_record_id,
        )
        assert record.content_sha256 == revision_sha256
        records.append(record)
    if records:
        await store.ingest_batch(
            operation_id,
            records,
            stream_key=f"test-evidence-{operation_id}",
            next_cursor=f"cursor-{operation_id}",
        )


async def _finding_rows(store: OperationMemoryStore, operation_id: str):
    async with store._lock:
        db = store._connection()
        result = {}
        for table in ("findings", "finding_view", "finding_notification_ledger"):
            rows = await (
                await db.execute(
                    f"SELECT * FROM {table} WHERE operation_id = ? ORDER BY rowid",
                    (operation_id,),
                )
            ).fetchall()
            result[table] = [tuple(row) for row in rows]
        return result


def test_evidence_must_resolve_under_the_same_operation_before_any_write(tmp_path):
    async def scenario():
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        candidate7 = _candidate(1, operation_id="7")
        await _ingest_candidate_evidence(store, "8", [_candidate(1, operation_id="8")])

        with pytest.raises(ValueError, match="evidence pointer does not resolve"):
            await reconcile_findings(store, "7", [candidate7])
        assert await _finding_rows(store, "7") == {
            "findings": [],
            "finding_view": [],
            "finding_notification_ledger": [],
        }

        await _ingest_candidate_evidence(store, "7", [candidate7])
        admitted = await reconcile_findings(store, "7", [candidate7])
        assert admitted.view[0].finding_id == candidate7.finding_id

        missing = replace(
            candidate7,
            evidence=(replace(candidate7.evidence[0], revision_sha256="f" * 64),),
        )
        with pytest.raises(ValueError, match="evidence pointer does not resolve"):
            await reconcile_findings(store, "7", [missing])
        await store.close()

    asyncio.run(scenario())


def test_admission_guard_failure_rolls_back_entire_reconcile_transaction(tmp_path):
    class OwnerArchived(RuntimeError):
        pass

    async def scenario():
        store = OperationMemoryStore(tmp_path / "guarded-memory.db")
        await store.initialize()
        candidate = _candidate(1)
        await _ingest_candidate_evidence(store, "7", [candidate])
        guard_calls = 0

        async def deny_at_commit():
            nonlocal guard_calls
            guard_calls += 1
            raise OwnerArchived("owner archived before commit")

        with pytest.raises(OwnerArchived, match="before commit"):
            await reconcile_findings(
                store,
                "7",
                [candidate],
                admission_guard=deny_at_commit,
            )
        assert guard_calls == 1
        async with store._lock:
            db = store._connection()
            for table in (
                "findings",
                "finding_view",
                "finding_notification_ledger",
                "finding_delivery_outbox",
            ):
                count = (await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone())[0]
                assert count == 0, table
        await store.close()

    asyncio.run(scenario())


def test_identical_finding_replay_is_byte_stable(tmp_path):
    async def scenario():
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        candidate = _candidate(1)
        await _ingest_candidate_evidence(store, "7", [candidate])
        await reconcile_findings(store, "7", [candidate])
        before = await _finding_rows(store, "7")

        replay = await reconcile_findings(store, "7", [candidate])

        assert replay.notification is None
        assert await _finding_rows(store, "7") == before
        await store.close()

    asyncio.run(scenario())


def _candidate(
    number: int,
    *,
    operation_id: str = "7",
    score: float | None = None,
    state: FindingState = FindingState.NEW,
    evidence: tuple[EvidencePointer, ...] | None = None,
) -> FindingCandidate:
    return FindingCandidate.build(
        operation_id=operation_id,
        finding_key=f"periodic-write:host-{number}:job-{number}",
        finding_type="privileged_periodic_write",
        title=f"Finding {number}",
        state=state,
        score=float(score if score is not None else number),
        observed_at_utc=f"2026-08-{number:02d}T00:00:00Z",
        confidence=0.8,
        evidence=evidence or (_evidence(number),),
        missing_assumptions=("effective write remains available",),
        rationale="Two typed source facts share the exact host and path.",
        suggested_validation="Review the exact source tasks in supervised chat.",
    )


def test_schema_and_stable_identity_survive_evidence_growth(tmp_path):
    async def scenario():
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        first = _candidate(1)
        await _ingest_candidate_evidence(store, "7", [first])
        initial = await reconcile_findings(store, "7", [first])
        assert initial.notification is not None
        assert len(initial.view) == 1
        item = initial.view[0]
        assert item.finding_id == stable_finding_id("7", first.finding_key)
        assert item.rank == 1
        assert item.state is FindingState.NEW
        assert item.confidence == 0.8
        assert item.evidence[0]["source_record_id"] == "1"
        assert item.missing_assumptions == ("effective write remains available",)
        assert item.rationale
        assert item.suggested_validation

        stronger = replace(first, evidence=(_evidence(1), _evidence(2, callback="9")))
        await _ingest_candidate_evidence(store, "7", [stronger])
        changed = await reconcile_findings(store, "7", [stronger])
        assert changed.view[0].finding_id == item.finding_id
        assert changed.notification is not None
        assert [entry["kind"] for entry in changed.notification.changes] == ["evidence"]
        assert len(await list_notification_events(store, "7")) == 2
        await store.close()

    asyncio.run(scenario())


def test_top_five_is_deterministic_and_rank_only_shuffle_is_silent(tmp_path):
    async def scenario():
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        candidates = [_candidate(number) for number in range(1, 7)]
        await _ingest_candidate_evidence(store, "7", candidates)
        first = await reconcile_findings(store, "7", reversed(candidates))
        assert len(first.view) == 5
        assert [item.rank for item in first.view] == [1, 2, 3, 4, 5]
        assert [item.score for item in first.view] == [6, 5, 4, 3, 2]
        assert first.notification is not None

        unchanged = await reconcile_findings(store, "7", candidates)
        assert unchanged.notification is None

        swapped = [
            replace(candidate, score=10.0 if candidate.finding_key.endswith("host-2:job-2") else candidate.score)
            for candidate in candidates
        ]
        reranked = await reconcile_findings(store, "7", swapped)
        assert reranked.notification is None
        assert reranked.view[0].finding_key == "periodic-write:host-2:job-2"
        assert {item.finding_id for item in reranked.view} == {
            item.finding_id for item in first.view
        }
        assert len(await list_notification_events(store, "7")) == 1
        await store.close()

    asyncio.run(scenario())


def test_state_change_and_membership_change_emit_one_event(tmp_path):
    async def scenario():
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        candidates = [_candidate(number) for number in range(1, 7)]
        await _ingest_candidate_evidence(store, "7", candidates)
        await reconcile_findings(store, "7", candidates)
        top = _candidate(6, state=FindingState.RESOLVED)
        result = await reconcile_findings(store, "7", [top])
        assert result.notification is not None
        kinds = [change["kind"] for change in result.notification.changes]
        assert kinds.count("state") == 1
        assert kinds.count("membership") == 2
        assert len(await list_notification_events(store, "7")) == 2
        assert top.finding_id not in {item.finding_id for item in result.view}
        await store.close()

    asyncio.run(scenario())


def test_restart_operation_isolation_and_wipe_cascade(tmp_path):
    async def scenario():
        db_path = tmp_path / "memory.db"
        store = OperationMemoryStore(db_path)
        await store.initialize()
        op7 = _candidate(1, operation_id="7")
        op8 = _candidate(1, operation_id="8")
        assert op7.finding_id != op8.finding_id
        await _ingest_candidate_evidence(store, "7", [op7])
        await _ingest_candidate_evidence(store, "8", [op8])
        await reconcile_findings(store, "7", [op7])
        await reconcile_findings(store, "8", [op8])
        await store.close()

        resumed = OperationMemoryStore(db_path)
        await resumed.initialize()
        assert [item.finding_id for item in await current_findings_view(resumed, "7")] == [
            op7.finding_id
        ]
        assert [item.finding_id for item in await current_findings_view(resumed, "8")] == [
            op8.finding_id
        ]
        repeated = await reconcile_findings(resumed, "7", [op7])
        assert repeated.notification is None
        assert await resumed.wipe_operation("7") is True
        assert await current_findings_view(resumed, "7") == ()
        assert await list_notification_events(resumed, "7") == ()
        assert len(await current_findings_view(resumed, "8")) == 1
        await resumed.close()

    asyncio.run(scenario())


def test_conflicting_duplicate_batch_fails_without_partial_write(tmp_path):
    async def scenario():
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        candidate = _candidate(1)
        with pytest.raises(ValueError, match="conflicting duplicate"):
            await reconcile_findings(store, "7", [candidate, replace(candidate, title="Conflict")])
        assert await current_findings_view(store, "7") == ()
        await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("finding_key", "Not Normalized", "normalized lowercase"),
        ("state", "open", "invalid finding state"),
        ("score", float("inf"), "score must be finite"),
        ("confidence", 1.1, "confidence must be between"),
        ("evidence", (), "at least one exact evidence"),
    ],
)
def test_candidate_validation_fails_closed(field, value, message):
    values = {
        "operation_id": "7",
        "finding_key": "periodic-write:host-1:job-1",
        "finding_type": "privileged_periodic_write",
        "title": "Finding",
        "state": FindingState.NEW,
        "score": 1.0,
        "observed_at_utc": "2026-08-01T00:00:00Z",
        "confidence": 0.8,
        "evidence": (_evidence(1),),
        "missing_assumptions": (),
        "rationale": "Reason",
        "suggested_validation": "Validate",
    }
    values[field] = value
    with pytest.raises(ValueError, match=message):
        FindingCandidate.build(**values)


def test_evidence_pointer_requires_exact_revision_hash():
    with pytest.raises(ValueError, match="64 lowercase hex"):
        EvidencePointer.build(
            record_class="task_output",
            source_record_id="1",
            revision_sha256="not-a-hash",
        )


def test_candidate_identity_cannot_be_detached_from_typed_key():
    candidate = _candidate(1)
    with pytest.raises(ValueError, match="derive only"):
        replace(candidate, finding_key="periodic-write:host-2:job-2")


def test_direct_candidate_construction_canonicalizes_exact_evidence(tmp_path):
    async def scenario():
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        first = _candidate(1, evidence=(_evidence(1), _evidence(2)))
        await _ingest_candidate_evidence(store, "7", [first])
        await reconcile_findings(store, "7", [first])

        reordered = replace(
            first,
            evidence=(_evidence(2), _evidence(1), _evidence(1)),
        )
        assert reordered.evidence == first.evidence
        replay = await reconcile_findings(store, "7", [reordered])
        assert replay.notification is None
        assert len(replay.view[0].evidence) == 2

        equivalent_batch = await reconcile_findings(store, "7", [first, reordered])
        assert equivalent_batch.notification is None
        await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_class", 1),
        ("source_record_id", 1),
        ("revision_sha256", 1),
        ("callback_display_id", 1),
        ("task_display_id", 1),
        ("task_output_id", 1),
    ],
)
def test_direct_evidence_pointer_rejects_non_string_fields(field, value):
    values = {
        "record_class": "task_output",
        "source_record_id": "1",
        "revision_sha256": "1" * 64,
        "callback_display_id": "4",
        "task_display_id": "101",
        "task_output_id": "1",
    }
    values[field] = value
    with pytest.raises(ValueError, match=f"{field} must be a string"):
        EvidencePointer(**values)


def test_direct_candidate_rejects_non_string_missing_assumption():
    candidate = _candidate(1)
    with pytest.raises(ValueError, match="missing assumption must be a string"):
        replace(candidate, missing_assumptions=(7,))


def test_builder_keeps_numeric_display_ids_as_typed_strings():
    pointer = EvidencePointer.build(
        record_class="task_output",
        source_record_id=1,
        revision_sha256="1" * 64,
        callback_display_id=4,
        task_display_id=101,
        task_output_id=1,
    )
    assert pointer.source_record_id == "1"
    assert pointer.callback_display_id == "4"
    assert pointer.task_display_id == "101"
    assert pointer.task_output_id == "1"


@pytest.mark.parametrize(
    "container",
    [
        "whole assumption",
        b"whole assumption",
        bytearray(b"whole assumption"),
        {"whole assumption": False},
    ],
)
def test_assumptions_reject_scalar_and_mapping_collections(container):
    candidate = _candidate(1)
    with pytest.raises(ValueError, match="missing_assumptions must be a non-scalar iterable"):
        replace(candidate, missing_assumptions=container)

    with pytest.raises(ValueError, match="missing_assumptions must be a non-scalar iterable"):
        FindingCandidate.build(
            operation_id="7",
            finding_key="periodic-write:host-1:job-1",
            finding_type="privileged_periodic_write",
            title="Finding",
            state=FindingState.NEW,
            score=1.0,
            observed_at_utc="2026-08-01T00:00:00Z",
            confidence=0.8,
            evidence=(_evidence(1),),
            missing_assumptions=container,
            rationale="Reason",
            suggested_validation="Validate",
        )


@pytest.mark.parametrize(
    "container",
    [
        "not evidence",
        b"not evidence",
        bytearray(b"not evidence"),
        {_evidence(1): False},
    ],
)
def test_evidence_rejects_scalar_and_mapping_collections(container):
    candidate = _candidate(1)
    with pytest.raises(ValueError, match="evidence must be a non-scalar iterable"):
        replace(candidate, evidence=container)

    with pytest.raises(ValueError, match="evidence must be a non-scalar iterable"):
        FindingCandidate.build(
            operation_id="7",
            finding_key="periodic-write:host-1:job-1",
            finding_type="privileged_periodic_write",
            title="Finding",
            state=FindingState.NEW,
            score=1.0,
            observed_at_utc="2026-08-01T00:00:00Z",
            confidence=0.8,
            evidence=container,
            missing_assumptions=(),
            rationale="Reason",
            suggested_validation="Validate",
        )


@pytest.mark.parametrize("collection_type", [list, tuple, iter])
def test_valid_collection_shapes_preserve_whole_atoms(collection_type):
    evidence_values = [_evidence(2), _evidence(1), _evidence(1)]
    assumption_values = ["second assumption", "first assumption", "first assumption"]
    candidate = FindingCandidate.build(
        operation_id="7",
        finding_key="periodic-write:host-1:job-1",
        finding_type="privileged_periodic_write",
        title="Finding",
        state=FindingState.NEW,
        score=1.0,
        observed_at_utc="2026-08-01T00:00:00Z",
        confidence=0.8,
        evidence=collection_type(evidence_values),
        missing_assumptions=collection_type(assumption_values),
        rationale="Reason",
        suggested_validation="Validate",
    )

    assert candidate.evidence == (_evidence(1), _evidence(2))
    assert candidate.missing_assumptions == (
        "first assumption",
        "second assumption",
    )


@pytest.mark.parametrize("collection_type", [list, tuple, iter])
def test_empty_assumption_collection_remains_valid(collection_type):
    candidate = replace(
        _candidate(1),
        missing_assumptions=collection_type([]),
    )
    assert candidate.missing_assumptions == ()


@pytest.mark.parametrize("collection_type", [list, tuple, iter])
@pytest.mark.parametrize("bad_member", ["wrong", 7, {}, []])
def test_evidence_member_validation_matches_direct_and_builder(
    collection_type,
    bad_member,
):
    candidate = _candidate(1)
    with pytest.raises(ValueError, match="at least one exact evidence pointer"):
        replace(
            candidate,
            evidence=collection_type([_evidence(1), bad_member]),
        )

    with pytest.raises(ValueError, match="at least one exact evidence pointer"):
        FindingCandidate.build(
            operation_id="7",
            finding_key="periodic-write:host-1:job-1",
            finding_type="privileged_periodic_write",
            title="Finding",
            state=FindingState.NEW,
            score=1.0,
            observed_at_utc="2026-08-01T00:00:00Z",
            confidence=0.8,
            evidence=collection_type([_evidence(1), bad_member]),
            missing_assumptions=(),
            rationale="Reason",
            suggested_validation="Validate",
        )


@pytest.mark.parametrize("collection_type", [list, tuple, iter])
@pytest.mark.parametrize("bad_member", [7, {}, _evidence(2)])
def test_assumption_member_validation_matches_direct_and_builder(
    collection_type,
    bad_member,
):
    candidate = _candidate(1)
    with pytest.raises(ValueError, match="missing assumption must be a string"):
        replace(
            candidate,
            missing_assumptions=collection_type(["valid assumption", bad_member]),
        )

    with pytest.raises(ValueError, match="missing assumption must be a string"):
        FindingCandidate.build(
            operation_id="7",
            finding_key="periodic-write:host-1:job-1",
            finding_type="privileged_periodic_write",
            title="Finding",
            state=FindingState.NEW,
            score=1.0,
            observed_at_utc="2026-08-01T00:00:00Z",
            confidence=0.8,
            evidence=(_evidence(1),),
            missing_assumptions=collection_type(
                ["valid assumption", bad_member]
            ),
            rationale="Reason",
            suggested_validation="Validate",
        )
