from __future__ import annotations

import asyncio
import hashlib

import pytest

import sage_chat.mythic_findings_delivery as delivery_module
from sage_chat.mythic_findings_delivery import (
    FINDINGS_CHANNEL_NAME,
    GENERIC_MYTHIC_NOTICE,
    MythicFindingsDelivery,
    REQUIRED_WATCHER_EFFECTIVE_SCOPES,
    REQUIRED_WATCHER_SCOPES,
    WatcherBotIdentity,
    WatcherConfigurationError,
    WatcherMythicSession,
)
from sage_chat.watcher_control import WatcherControlPlane
from sage_chat.operation_findings import (
    EvidencePointer,
    FindingCandidate,
    FindingState,
    current_findings_view,
    list_pending_finding_deliveries,
    reconcile_findings,
)
from sage_chat.operation_memory import OperationMemoryStore, SourceRecord


def _candidate() -> tuple[FindingCandidate, SourceRecord]:
    content = "native evidence"
    record = SourceRecord.build(
        operation_id="7",
        record_class="task_output",
        source_record_id="1",
        observed_at_utc="2026-08-01T00:00:00Z",
        content=content,
        callback_display_id="4",
        task_display_id="11",
        task_output_id="1",
    )
    candidate = FindingCandidate.build(
        operation_id="7",
        finding_key="candidate-1",
        finding_type="privileged-writable-execution-target",
        title="Finding",
        state=FindingState.NEW,
        score=90,
        observed_at_utc="2026-08-01T00:00:00Z",
        confidence=0.7,
        evidence=(
            EvidencePointer.build(
                record_class="task_output",
                source_record_id="1",
                revision_sha256=hashlib.sha256(content.encode()).hexdigest(),
                callback_display_id="4",
                task_display_id="11",
                task_output_id="1",
            ),
        ),
        missing_assumptions=("binding remains current",),
        rationale="Reasoned value",
        suggested_validation="Supervised review",
    )
    return candidate, record


async def _seed(store: OperationMemoryStore):
    candidate, record = _candidate()
    await store.ingest_batch("7", [record], stream_key="test", next_cursor="1")
    await reconcile_findings(store, "7", [candidate])
    return await store.snapshot("7")


def _runtime_whoami(**changes):
    grants = [
        "callback.read", "chat-ai.read", "chat.write", "credential.read",
        "eventlog.write", "file.read", "operation.read", "response.read",
        "task.read",
    ]
    row = {
        "status": "success",
        "user_id": 4,
        "username": "sage-bot",
        "account_type": "bot",
        "active": True,
        "deleted": False,
        "current_operation_id": 7,
        "scopes": grants,
        "effective_scopes": [*grants, "chat.read", "eventlog.read"],
    }
    row.update(changes)
    return row


def test_runtime_scope_class_reads_ai_owner_without_redundant_standard_read_grant():
    assert REQUIRED_WATCHER_SCOPES == {
        "callback.read",
        "chat-ai.read",
        "chat.write",
        "credential.read",
        "eventlog.write",
        "file.read",
        "operation.read",
        "response.read",
        "task.read",
    }
    assert "chat.read" not in REQUIRED_WATCHER_SCOPES
    assert "chat-ai.write" not in REQUIRED_WATCHER_SCOPES
    assert REQUIRED_WATCHER_EFFECTIVE_SCOPES == {
        *REQUIRED_WATCHER_SCOPES,
        "chat.read",
        "eventlog.read",
    }


def test_exact_runtime_scope_class_reaches_bound_ai_owner_read(monkeypatch):
    async def scenario():
        monkeypatch.setenv("SAGE_WATCHER_APITOKEN", "secret-not-logged")
        monkeypatch.setenv("NGINX_HOST", "mythic")
        client = {"grants": set(_runtime_whoami()["scopes"])}

        async def login(**_kwargs):
            return client

        async def execute(observed_client, query, _variables):
            if "SageWatcherWhoami" in query:
                return {"whoami": _runtime_whoami()}
            if "SageWatcherChannel" in query:
                rows = []
                if "chat-ai.read" in observed_client["grants"]:
                    rows = [{
                        "id": 41,
                        "operation_id": 7,
                        "name": "watcher-owner",
                        "channel_type": "ai",
                        "chat_model": "Sage Watcher",
                        "locked": True,
                        "archived": False,
                        "apitokens_id": 9,
                        "ai_metadata": {"config": {}, "channel_metadata": {}},
                        "chat_container": {"name": "sage"},
                    }]
                return {"chat_channel": rows}
            raise AssertionError("unexpected query")

        session = await MythicFindingsDelivery(
            login=login, execute=execute
        ).connect("7")
        owner = await WatcherControlPlane(execute=execute).inspect_channel(
            session.client, channel_id=41, operation_id=7
        )
        assert owner.valid_owner_candidate
        assert owner.channel_id == 41

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("whoami", "match"),
    [
        (_runtime_whoami(account_type="user"), "active bot"),
        (_runtime_whoami(current_operation_id=8), "active bot"),
        (_runtime_whoami(scopes=["chat.write"]), "stored grants"),
        (_runtime_whoami(scopes=["*"]), "stored grants"),
    ],
)
def test_connect_fails_closed_for_wrong_identity_operation_or_scopes(
    tmp_path, monkeypatch, whoami, match
):
    async def scenario():
        monkeypatch.setenv("SAGE_WATCHER_APITOKEN", "secret-not-logged")
        monkeypatch.setenv("NGINX_HOST", "mythic")

        async def login(**_kwargs):
            return object()

        async def execute(_client, _query, _variables):
            return {"whoami": whoami}

        with pytest.raises(WatcherConfigurationError, match=match):
            await MythicFindingsDelivery(login=login, execute=execute).connect("7")

    asyncio.run(scenario())


def test_durable_outbox_retries_only_failed_sink_and_posts_full_state_to_standard_chat(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.delenv("SAGE_FINDINGS_SLACK_WEBHOOK_URL", raising=False)
        store = OperationMemoryStore(tmp_path / "memory.db")
        await store.initialize()
        snapshot = await _seed(store)
        view = await current_findings_view(store, "7")
        calls = []
        message_attempts = 0

        async def execute(_client, query, variables):
            nonlocal message_attempts
            calls.append((query, variables))
            if "SageFindingsChannels" in query:
                return {"chat_channel": []}
            if "CreateSageFindingsChannel" in query:
                assert variables["name"] == FINDINGS_CHANNEL_NAME
                return {"chatCreateChannel": {"status": "success", "channel_id": 12}}
            if "CreateSageFindingsMessage" in query:
                message_attempts += 1
                assert "Exact evidence pointers" in variables["message"]
                if message_attempts == 1:
                    return {"chatCreateMessage": {"status": "error", "error": "down"}}
                return {"chatCreateMessage": {"status": "success", "message_id": 33}}
            raise AssertionError("unexpected query")

        eventlog_messages = []

        async def eventlog(_client, **kwargs):
            eventlog_messages.append(kwargs)
            return {"createOperationEventLog": {"status": "success"}}

        delivery = MythicFindingsDelivery(execute=execute, eventlog=eventlog)
        session = WatcherMythicSession(
            client=object(),
            identity=WatcherBotIdentity("7", 4, "sage-bot", ("*",)),
        )
        assert await delivery.drain(
            store, "7", session, view=view, snapshot=snapshot
        ) == 2
        pending = await list_pending_finding_deliveries(store, "7")
        assert [(row.sink, row.attempts) for row in pending] == [("mythic_chat", 1)]
        assert eventlog_messages == [
            {
                "message": GENERIC_MYTHIC_NOTICE,
                "level": "info",
                "source": "sage-findings-watcher",
                "warning": True,
            }
        ]

        assert await delivery.drain(
            store, "7", session, view=view, snapshot=await store.snapshot("7")
        ) == 1
        assert await list_pending_finding_deliveries(store, "7") == ()
        assert message_attempts == 2
        assert len(eventlog_messages) == 1
        await store.close()

    asyncio.run(scenario())


def test_delivery_guard_failure_precedes_every_external_sink_and_retry_mutation(
    tmp_path, monkeypatch
):
    class OwnerArchived(RuntimeError):
        pass

    async def scenario():
        monkeypatch.delenv("SAGE_FINDINGS_SLACK_WEBHOOK_URL", raising=False)
        store = OperationMemoryStore(tmp_path / "guarded-delivery.db")
        await store.initialize()
        snapshot = await _seed(store)
        view = await current_findings_view(store, "7")
        external_effects = []

        async def execute(_client, _query, _variables):
            external_effects.append("graphql")
            raise AssertionError("delivery crossed a failed owner guard")

        async def eventlog(_client, **_kwargs):
            external_effects.append("eventlog")
            raise AssertionError("delivery crossed a failed owner guard")

        async def deny_at_sink():
            raise OwnerArchived("owner archived at drain entry")

        delivery = MythicFindingsDelivery(execute=execute, eventlog=eventlog)
        session = WatcherMythicSession(
            client=object(),
            identity=WatcherBotIdentity("7", 4, "sage-bot", ("*",)),
        )
        with pytest.raises(OwnerArchived, match="drain entry"):
            await delivery.drain(
                store,
                "7",
                session,
                view=view,
                snapshot=snapshot,
                admission_guard=deny_at_sink,
            )
        assert external_effects == []
        pending = await list_pending_finding_deliveries(store, "7")
        assert len(pending) == 3
        assert all(row.attempts == 0 and not row.last_error for row in pending)
        await store.close()

    asyncio.run(scenario())


def test_sink_accept_cancel_before_ack_reuses_stable_outbox_identity(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.delenv("SAGE_FINDINGS_SLACK_WEBHOOK_URL", raising=False)
        store = OperationMemoryStore(tmp_path / "ambiguous-delivery.db")
        await store.initialize()
        snapshot = await _seed(store)
        view = await current_findings_view(store, "7")
        pending_before = await list_pending_finding_deliveries(store, "7")
        first_identity = (
            pending_before[0].notification.ledger_id,
            pending_before[0].sink,
        )
        effects = []
        acknowledgement_entered = asyncio.Event()
        original_record = delivery_module.record_finding_delivery_attempt
        delay_first_ack = True

        async def external_effect(pending, _session, _markdown):
            effects.append((pending.notification.ledger_id, pending.sink))

        async def delayed_record(*args, **kwargs):
            nonlocal delay_first_ack
            if kwargs.get("delivered") and delay_first_ack:
                delay_first_ack = False
                acknowledgement_entered.set()
                await asyncio.Event().wait()
            return await original_record(*args, **kwargs)

        delivery = MythicFindingsDelivery()
        delivery._deliver_sink = external_effect
        monkeypatch.setattr(
            delivery_module,
            "record_finding_delivery_attempt",
            delayed_record,
        )
        session = WatcherMythicSession(
            client=object(),
            identity=WatcherBotIdentity("7", 4, "sage-bot", ("*",)),
        )
        attempt = asyncio.create_task(
            delivery.drain(store, "7", session, view=view, snapshot=snapshot)
        )
        await asyncio.wait_for(acknowledgement_entered.wait(), timeout=1)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        pending_after_cancel = await list_pending_finding_deliveries(store, "7")
        assert (
            pending_after_cancel[0].notification.ledger_id,
            pending_after_cancel[0].sink,
        ) == first_identity

        monkeypatch.setattr(
            delivery_module,
            "record_finding_delivery_attempt",
            original_record,
        )
        assert await delivery.drain(
            store,
            "7",
            session,
            view=view,
            snapshot=await store.snapshot("7"),
        ) == len(pending_before)
        assert effects.count(first_identity) == 2
        assert await list_pending_finding_deliveries(store, "7") == ()
        await store.close()

    asyncio.run(scenario())


def test_connect_accepts_exact_operation_bot_with_required_scopes(monkeypatch):
    async def scenario():
        monkeypatch.setenv("SAGE_WATCHER_APITOKEN", "secret-not-logged")
        monkeypatch.setenv("NGINX_HOST", "mythic")
        observed = []

        async def login(**kwargs):
            observed.append(kwargs)
            return object()

        async def execute(_client, query, _variables):
            assert "auth_method scopes effective_scopes" in query
            return {"whoami": _runtime_whoami()}

        session = await MythicFindingsDelivery(login=login, execute=execute).connect("7")
        assert session.identity.username == "sage-bot"
        assert observed[0]["apitoken"] == "secret-not-logged"
        assert observed[0]["server_ip"] == "mythic"

    asyncio.run(scenario())


def test_connect_normalizes_scope_order_and_case_without_weakening_exact_sets(monkeypatch):
    async def scenario():
        monkeypatch.setenv("SAGE_WATCHER_APITOKEN", "secret-not-logged")
        monkeypatch.setenv("NGINX_HOST", "mythic")
        base = _runtime_whoami()

        async def login(**_kwargs):
            return object()

        async def execute(_client, _query, _variables):
            return {
                "whoami": _runtime_whoami(
                    scopes=[scope.upper() for scope in reversed(base["scopes"])],
                    effective_scopes=[
                        scope.upper() for scope in reversed(base["effective_scopes"])
                    ],
                )
            }

        session = await MythicFindingsDelivery(login=login, execute=execute).connect("7")
        assert set(session.identity.effective_scopes) == set(base["effective_scopes"])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"scopes": ["*"]}, "stored grants"),
        ({"scopes": ["callback.read"]}, "stored grants"),
        ({"scopes": [*_runtime_whoami()["scopes"], "task.write"]}, "stored grants"),
        ({"scopes": [*_runtime_whoami()["scopes"], "CALLBACK.READ"]}, "malformed"),
        ({"scopes": "callback.read"}, "malformed"),
        ({"scopes": [*_runtime_whoami()["scopes"], 3]}, "malformed"),
        ({"effective_scopes": _runtime_whoami()["scopes"]}, "effective scopes"),
        ({"effective_scopes": [*_runtime_whoami()["effective_scopes"], "task.write"]}, "effective scopes"),
        ({"effective_scopes": ["*"]}, "effective scopes"),
        ({"effective_scopes": "callback.read"}, "malformed"),
        ({"effective_scopes": [*_runtime_whoami()["effective_scopes"], None]}, "malformed"),
    ],
)
def test_connect_separates_exact_stored_grants_from_effective_scope_closure(
    monkeypatch, changes, match
):
    async def scenario():
        monkeypatch.setenv("SAGE_WATCHER_APITOKEN", "secret-not-logged")
        monkeypatch.setenv("NGINX_HOST", "mythic")

        async def login(**_kwargs):
            return object()

        async def execute(_client, _query, _variables):
            return {"whoami": _runtime_whoami(**changes)}

        with pytest.raises(WatcherConfigurationError, match=match):
            await MythicFindingsDelivery(login=login, execute=execute).connect("7")

    asyncio.run(scenario())


def test_ensure_channel_eagerly_creates_once_without_a_pending_finding():
    async def scenario():
        calls = []

        async def execute(_client, query, variables):
            calls.append((query, variables))
            if "SageFindingsChannels" in query:
                return {"chat_channel": []}
            if "CreateSageFindingsChannel" in query:
                return {
                    "chatCreateChannel": {
                        "status": "success",
                        "channel_id": 12,
                    }
                }
            raise AssertionError("channel startup must not post a message")

        delivery = MythicFindingsDelivery(execute=execute)
        session = WatcherMythicSession(
            client=object(),
            identity=WatcherBotIdentity("7", 4, "sage-bot", ("*",)),
        )
        assert await delivery.ensure_channel(session) == 12
        assert await delivery.ensure_channel(session) == 12
        assert len(calls) == 2

    asyncio.run(scenario())


def test_ensure_channel_reuses_same_name_standard_channel_with_legacy_description():
    async def scenario():
        calls = []

        async def execute(_client, query, variables):
            calls.append((query, variables))
            if "SageFindingsChannels" in query:
                return {
                    "chat_channel": [
                        {
                            "id": 12,
                            "name": FINDINGS_CHANNEL_NAME,
                            "description": "Created by an earlier Sage build.",
                            "channel_type": "standard",
                            "archived": False,
                        }
                    ]
                }
            raise AssertionError("same-name channel must be reused, not created")

        delivery = MythicFindingsDelivery(execute=execute)
        session = WatcherMythicSession(
            client=object(),
            identity=WatcherBotIdentity("7", 4, "sage-bot", ("*",)),
        )
        assert await delivery.ensure_channel(session) == 12
        assert len(calls) == 1
        _, variables = calls[0]
        assert variables == {"name": FINDINGS_CHANNEL_NAME}

    asyncio.run(scenario())


def test_ensure_channel_fails_closed_for_multiple_same_name_standard_channels():
    async def scenario():
        async def execute(_client, query, _variables):
            if "SageFindingsChannels" in query:
                return {
                    "chat_channel": [
                        {"id": 12, "name": FINDINGS_CHANNEL_NAME},
                        {"id": 13, "name": FINDINGS_CHANNEL_NAME},
                    ]
                }
            raise AssertionError("duplicate same-name channels must not be mutated")

        delivery = MythicFindingsDelivery(execute=execute)
        session = WatcherMythicSession(
            client=object(),
            identity=WatcherBotIdentity("7", 4, "sage-bot", ("*",)),
        )
        with pytest.raises(Exception, match="multiple managed Sage findings channels"):
            await delivery.ensure_channel(session)

    asyncio.run(scenario())


def _on_start_whoami(**changes):
    row = {
        "status": "success",
        "user_id": 4,
        "username": "sage-bot",
        "account_type": "bot",
        "active": True,
        "deleted": False,
        "current_operation_id": 7,
        "auth_method": "on_start",
        "effective_scopes": [
            "callback.read", "callback.write", "chat.read", "chat.write",
            "chat-ai.read", "chat-ai.write", "eventing.read", "eventing.write",
            "file.read", "file.write", "payload.read", "payload.write",
            "tag.read", "tag.write",
        ],
    }
    row.update(changes)
    return row


def test_on_start_bootstrap_crosses_shared_outer_create_scope_without_persistent_expansion(
    monkeypatch,
):
    async def scenario():
        monkeypatch.setenv("SAGE_WATCHER_APITOKEN", "persistent-sentinel")
        monkeypatch.setenv("NGINX_HOST", "mythic")
        bootstrap_sentinel = "short-lived-sentinel"
        calls = []

        async def login(**kwargs):
            return {"token": kwargs["apitoken"]}

        async def execute(client, query, variables):
            calls.append((client["token"], query, variables))
            if "SageWatcherWhoami" in query:
                return {
                    "whoami": (
                        _on_start_whoami()
                        if client["token"] == bootstrap_sentinel
                        else {
                            **_runtime_whoami(),
                        }
                    )
                }
            if "SageFindingsChannels" in query:
                return {"chat_channel": []}
            if "CreateSageFindingsChannel" in query:
                scopes = (
                    set(_on_start_whoami()["effective_scopes"])
                    if client["token"] == bootstrap_sentinel
                    else set(REQUIRED_WATCHER_SCOPES)
                )
                if not {"chat.write", "chat-ai.write"}.issubset(scopes):
                    raise RuntimeError("outer shared channel-create scope denied")
                assert variables == {
                    "name": FINDINGS_CHANNEL_NAME,
                    "description": "Sage operation findings (managed by sage-findings-watcher-v1).",
                }
                assert all(
                    field not in query
                    for field in (
                        "chat_container_id", "apitokens_id", "chat_model", "ai_metadata"
                    )
                )
                return {"chatCreateChannel": {"status": "success", "channel_id": 12}}
            raise AssertionError("unexpected operation")

        delivery = MythicFindingsDelivery(login=login, execute=execute)
        persistent = await delivery.connect("7")
        with pytest.raises(RuntimeError, match="outer shared"):
            await delivery.ensure_channel(persistent)

        assert await delivery.bootstrap_channel(
            "7", bootstrap_token=bootstrap_sentinel, server_name="mythic"
        ) == 12
        call_count = len(calls)
        assert await delivery.ensure_channel(persistent) == 12
        assert len(calls) == call_count
        assert bootstrap_sentinel not in repr(vars(delivery))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "whoami",
    [
        _on_start_whoami(status="error"),
        _on_start_whoami(user_id=0),
        _on_start_whoami(account_type="user"),
        _on_start_whoami(active=False),
        _on_start_whoami(deleted=True),
        _on_start_whoami(current_operation_id=8),
        _on_start_whoami(auth_method="api"),
        _on_start_whoami(effective_scopes=["chat.write"]),
        _on_start_whoami(effective_scopes=["chat-ai.write"]),
        _on_start_whoami(effective_scopes=["chat.write", "chat-ai.write", "*"]),
        _on_start_whoami(
            effective_scopes=["chat.write", "chat-ai.write", "task.write"]
        ),
    ],
)
def test_bootstrap_fails_closed_for_invalid_or_overpowered_on_start_identity(whoami):
    async def scenario():
        execute_calls = 0

        async def login(**_kwargs):
            return object()

        async def execute(_client, _query, _variables):
            nonlocal execute_calls
            execute_calls += 1
            return {"whoami": whoami}

        with pytest.raises(WatcherConfigurationError):
            await MythicFindingsDelivery(login=login, execute=execute).bootstrap_channel(
                "7", bootstrap_token="short-lived-sentinel", server_name="mythic"
            )
        assert execute_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("whoami", [None, [], {"status": "success"}])
def test_bootstrap_fails_closed_for_malformed_whoami(whoami):
    async def scenario():
        async def execute(_client, _query, _variables):
            return {"whoami": whoami}

        with pytest.raises(WatcherConfigurationError):
            await MythicFindingsDelivery(
                login=lambda **_kwargs: asyncio.sleep(0, result=object()),
                execute=execute,
            ).bootstrap_channel(
                "7", bootstrap_token="short-lived-sentinel", server_name="mythic"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "create_result",
    [None, {}, {"status": "error", "error": "denied"}],
)
def test_bootstrap_fails_closed_for_malformed_create_result(create_result):
    async def scenario():
        async def execute(_client, query, _variables):
            if "SageWatcherWhoami" in query:
                return {"whoami": _on_start_whoami()}
            if "SageFindingsChannels" in query:
                return {"chat_channel": []}
            return {"chatCreateChannel": create_result}

        delivery = MythicFindingsDelivery(
            login=lambda **_kwargs: asyncio.sleep(0, result=object()),
            execute=execute,
        )
        with pytest.raises(Exception):
            await delivery.bootstrap_channel(
                "7", bootstrap_token="short-lived-sentinel", server_name="mythic"
            )
        assert "short-lived-sentinel" not in repr(vars(delivery))

    asyncio.run(scenario())


def test_bootstrap_reuses_legacy_description_and_duplicate_channels_fail_closed():
    async def run(rows):
        calls = []

        async def execute(_client, query, variables):
            calls.append((query, variables))
            if "SageWatcherWhoami" in query:
                return {"whoami": _on_start_whoami()}
            if "SageFindingsChannels" in query:
                return {"chat_channel": rows}
            raise AssertionError("bootstrap must not create another standard channel")

        delivery = MythicFindingsDelivery(
            login=lambda **_kwargs: asyncio.sleep(0, result=object()),
            execute=execute,
        )
        return delivery, calls, await delivery.bootstrap_channel(
            "7", bootstrap_token="short-lived-sentinel", server_name="mythic"
        )

    legacy = [{
        "id": 12, "name": FINDINGS_CHANNEL_NAME,
        "description": "Created by an earlier build.",
        "channel_type": "standard", "archived": False,
    }]
    delivery, calls, channel_id = asyncio.run(run(legacy))
    assert channel_id == 12
    assert len(calls) == 2
    assert "short-lived-sentinel" not in repr(vars(delivery))

    with pytest.raises(Exception, match="multiple managed Sage findings channels"):
        asyncio.run(run(legacy + [{"id": 13, "name": FINDINGS_CHANNEL_NAME}]))
