"""Nested HITL resume writes must not duplicate authenticated operator input.

LangGraph 1.2.16 stopped accumulating a resume's null-task writes, so a checkpoint written on a current LangGraph
resumes correctly through either saver. A checkpoint paused before the upgrade still carries the accumulated writes:
resumed through the installed saver it yields a second current operator input, and Sage's saver is what resumes it.
"""

import asyncio
import base64
import json
from pathlib import Path

import aiosqlite
import pytest
from langchain_core.messages import HumanMessage
from langgraph._internal._constants import NULL_TASK_ID
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ai.langgraph.model import Model, SageState, _current_assisted_operator_input
from ai.langgraph.sqlite_checkpointer import SageAsyncSqliteSaver


REQUEST_ID = "offline:nested-resume:1"
CONFIG = {"configurable": {"thread_id": "nested-resume"}}
# Rows of this scenario's checkpoint after the steer, written by LangGraph 1.2.10 (see the file's provenance).
PAUSED_BEFORE_UPGRADE = (Path(__file__).resolve().parent / "fixtures" / "checkpoints"
                         / "nested_resume_langgraph_1_2_10.json")


def _operator_message(text: str, revision: int) -> HumanMessage:
    metadata = {
        "_request_id": REQUEST_ID,
        "_operator_input": True,
        "_operator_input_revision": revision,
        "_seq": revision + 1,
    }
    if revision:
        metadata["_operator_steer"] = True
    return HumanMessage(content=text, additional_kwargs=metadata)


def _current_count(values) -> int:
    return sum(
        isinstance(message, HumanMessage)
        and message.additional_kwargs.get("_request_id") == REQUEST_ID
        and message.additional_kwargs.get("_operator_input_revision") == 1
        for message in values["supervisor_messages"]
    )


def _graph(saver, effects: list):
    async def worker(state):
        _current_assisted_operator_input(state)
        interrupt({"card": 1})
        interrupt({"card": 2})
        interrupt({"card": 3})
        effects.append("completed once")
        return {}

    child = (StateGraph(SageState).add_node("worker", worker)
             .add_edge(START, "worker").add_edge("worker", END).compile())
    return (StateGraph(SageState).add_node("child", child)
            .add_edge(START, "child").add_edge("child", END)
            .compile(checkpointer=saver))


async def _nested_resume(db_path, saver_type):
    connection = await aiosqlite.connect(db_path)
    effects = []
    try:
        graph = _graph(saver_type(connection), effects)
        initial = {
            "messages": [],
            "supervisor_messages": [_operator_message("initial", 0)],
            "_request_id": REQUEST_ID,
            "_request_lane": "supervised_workflow",
        }
        await graph.ainvoke(initial, CONFIG)
        await graph.ainvoke(Command(
            resume="respond",
            update={"supervisor_messages": [_operator_message("steer", 1)]},
        ), CONFIG)
        after_respond = await graph.aget_state(CONFIG)
        assert _current_count(after_respond.values) == 1
        assert _current_assisted_operator_input(after_respond.values) == "steer"

        await graph.ainvoke(Command(resume="accept-1"), CONFIG)
        after_first_accept = await graph.aget_state(CONFIG)
        assert _current_count(after_first_accept.values) == 1
        assert _current_assisted_operator_input(after_first_accept.values) == "steer"
        assert effects == []
        root_id = after_first_accept.config["configurable"]["checkpoint_id"]
        async with connection.execute(
            "SELECT idx, channel FROM writes WHERE checkpoint_ns='' "
            "AND checkpoint_id=? AND task_id=? ORDER BY idx",
            (root_id, NULL_TASK_ID),
        ) as cursor:
            pending = await cursor.fetchall()
        assert (-4, "__resume__") in pending
        assert [idx for idx, channel in pending if channel == "supervisor_messages"] == [0]

        await graph.ainvoke(Command(resume="accept-2"), CONFIG)
        assert effects == ["completed once"]
        assert (await graph.aget_state(CONFIG)).next == ()
    finally:
        await connection.close()


async def _restore_paused_before_upgrade(db_path) -> None:
    """A disposable database holding exactly the recorded rows, in the installed saver's schema."""
    recorded = json.loads(PAUSED_BEFORE_UPGRADE.read_text())
    async with aiosqlite.connect(db_path) as connection:
        await AsyncSqliteSaver(connection).setup()
        for table in ("checkpoints", "writes"):
            columns = list(recorded[table][0])
            await connection.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [[base64.b64decode(value["base64"]) if isinstance(value, dict) else value
                  for value in (row[column] for column in columns)] for row in recorded[table]],
            )
        await connection.commit()


async def _resume_paused_before_upgrade(db_path, saver_type) -> list[tuple]:
    """Accept the two remaining cards; after each, (current inputs, selected input or None, effects, finished)."""
    await _restore_paused_before_upgrade(db_path)
    connection = await aiosqlite.connect(db_path)
    effects, observed = [], []
    try:
        graph = _graph(saver_type(connection), effects)
        paused = await graph.aget_state(CONFIG)
        assert paused.next and _current_count(paused.values) == 1
        assert _current_assisted_operator_input(paused.values) == "steer"
        for index in (1, 2):
            await graph.ainvoke(Command(resume=f"accept-{index}"), CONFIG)
            snapshot = await graph.aget_state(CONFIG)
            try:
                selected = _current_assisted_operator_input(snapshot.values)
            except RuntimeError as exc:
                if "one exact current operator input" not in str(exc):
                    raise
                selected = None
            observed.append((_current_count(snapshot.values), selected, len(effects), not snapshot.next))
            if selected is None:
                break
    finally:
        await connection.close()
    return observed


@pytest.mark.parametrize("saver_type", [AsyncSqliteSaver, SageAsyncSqliteSaver], ids=["installed", "sage"])
def test_a_fresh_nested_checkpoint_resumes_with_one_operator_input(tmp_path, saver_type):
    asyncio.run(_nested_resume(tmp_path / "fresh.db", saver_type))


@pytest.mark.parametrize("saver_type, observed", [
    # The installed saver materializes a second current input on the first resume; the selector refuses it.
    (AsyncSqliteSaver, [(2, None, 0, False)]),
    (SageAsyncSqliteSaver, [(1, "steer", 0, False), (1, "steer", 1, True)]),
], ids=["installed", "sage"])
def test_a_checkpoint_paused_before_the_upgrade_needs_sages_saver(tmp_path, saver_type, observed):
    assert asyncio.run(_resume_paused_before_upgrade(tmp_path / "paused.db", saver_type)) == observed


def test_conflicting_same_revision_still_fails_closed():
    state = {
        "_request_id": REQUEST_ID,
        "_request_lane": "supervised_workflow",
        "supervisor_messages": [
            _operator_message("initial", 0),
            _operator_message("steer one", 1),
            _operator_message("steer two", 1),
        ],
    }
    with pytest.raises(RuntimeError, match="one exact current operator input"):
        _current_assisted_operator_input(state)


def test_special_and_non_null_writes_keep_existing_semantics(tmp_path):
    async def run():
        connection = await aiosqlite.connect(tmp_path / "semantics.db")
        saver = SageAsyncSqliteSaver(connection)
        try:
            config = {"configurable": {"thread_id": "t", "checkpoint_ns": "",
                                       "checkpoint_id": "c"}}
            await saver.aput_writes(config, [("__resume__", "first")], NULL_TASK_ID)
            await saver.aput_writes(config, [("__resume__", "second")], NULL_TASK_ID)
            await saver.aput_writes(config, [
                ("__resume__", "third"), ("__resume__", "last"),
            ], NULL_TASK_ID)
            await saver.aput_writes(config, [("ordinary", "first")], "worker-task")
            await saver.aput_writes(config, [("ordinary", "second")], "worker-task")
            async with connection.execute(
                "SELECT task_id, idx, channel, type, value FROM writes ORDER BY task_id, idx"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 2
            decoded = {(task_id, channel): saver.serde.loads_typed((kind, value))
                       for task_id, _idx, channel, kind, value in rows}
            assert decoded[(NULL_TASK_ID, "__resume__")] == "last"
            assert decoded[("worker-task", "ordinary")] == "first"
        finally:
            await connection.close()

    asyncio.run(run())


def test_null_snapshot_replacement_is_atomic_and_isolated(tmp_path):
    async def run():
        connection = await aiosqlite.connect(tmp_path / "isolated.db")
        saver = SageAsyncSqliteSaver(connection)
        try:
            config = {"configurable": {"thread_id": "t", "checkpoint_ns": "",
                                       "checkpoint_id": "current"}}
            other = {"configurable": {**config["configurable"],
                                      "checkpoint_id": "other"}}
            await saver.aput_writes(config, [("ordinary", "original")], NULL_TASK_ID)
            await saver.aput_writes(config, [("ordinary", "worker")], "worker-task")
            await saver.aput_writes(other, [("ordinary", "other")], NULL_TASK_ID)
            await connection.execute(
                "CREATE TRIGGER refuse_insert BEFORE INSERT ON writes "
                "WHEN NEW.channel='trip' BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
            )
            await connection.commit()
            with pytest.raises(Exception, match="forced failure"):
                await saver.aput_writes(config, [("trip", "new")], NULL_TASK_ID)
            await connection.execute("DROP TRIGGER refuse_insert")
            await connection.commit()

            async def rows():
                async with connection.execute(
                    "SELECT checkpoint_id, task_id, channel, type, value FROM writes "
                    "ORDER BY checkpoint_id, task_id"
                ) as cursor:
                    return [(checkpoint, task, channel,
                             saver.serde.loads_typed((kind, value)))
                            for checkpoint, task, channel, kind, value
                            in await cursor.fetchall()]

            rows_before = await rows()
            assert ("current", NULL_TASK_ID, "ordinary", "original") in rows_before
            assert ("current", "worker-task", "ordinary", "worker") in rows_before
            assert ("other", NULL_TASK_ID, "ordinary", "other") in rows_before
            await saver.aput_writes(config, [("ordinary", "replacement")], NULL_TASK_ID)
            rows_after = await rows()
            assert ("current", NULL_TASK_ID, "ordinary", "replacement") in rows_after
            assert ("current", NULL_TASK_ID, "ordinary", "original") not in rows_after
            assert ("current", "worker-task", "ordinary", "worker") in rows_after
            assert ("other", NULL_TASK_ID, "ordinary", "other") in rows_after
        finally:
            await connection.close()

    asyncio.run(run())


def test_model_constructs_the_sage_saver(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / "sage.db"))

    async def run():
        model = Model(
            provider="openai", model="offline-model", system_prompt="offline",
            config={"configurable": {"api_key": "offline-only"}}, task_id=0,
            agent_task_id="offline-saver", mode="supervised", operation_id=1,
            channel_id=1, apitoken_id=0,
        )
        assert isinstance(model.memory, SageAsyncSqliteSaver)

    asyncio.run(run())
